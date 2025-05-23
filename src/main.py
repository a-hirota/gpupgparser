"""PostgreSQL-GPU処理パイプライン メインモジュール"""

import time
import numpy as np
from typing import Dict, List, Optional, Any

from .pg_connector import connect_to_postgres, check_table_exists, get_table_info, get_table_row_count, get_binary_data, get_query_column_info
from .gpu_memory_manager_v2 import GPUMemoryManagerV2
from .gpu_parse_wrapper import parse_binary_chunk_gpu, detect_pg_header_size
from test.test_single_row_pg_parser import calculate_row_starts_cpu
from .gpu_decoder_v2 import GPUDecoderV2 as GPUDecoder
from .output_handler import OutputHandler
from .meta_fetch import ColumnMeta

class PgGpuProcessor:
    """PostgreSQLデータGPU処理の統合クラス"""

    # Update __init__ to use V2 classes if that's the current standard
    def __init__(self, dbname='postgres', user='postgres', password='postgres', host='localhost', parquet_output=None, block_size=None, thread_count=None):
        """初期化"""
        dsn = os.environ.get("GPUPASER_PG_DSN")
        if dsn:
             print("Using DSN from GPUPASER_PG_DSN environment variable.")
             self.conn = connect_to_postgres(dbname, user, password, host)
        else:
             self.conn = connect_to_postgres(dbname, user, password, host)

        self.memory_manager = GPUMemoryManagerV2()
        self.gpu_decoder = GPUDecoder()
        self.output_handler = OutputHandler(parquet_output)
        self.parquet_output = parquet_output
        self.block_size = block_size
        self.thread_count = thread_count
        
    def process_table_chunk(self, table_name: str, chunk_size: int, offset: int = 0, output_file: Optional[str] = None):
        """テーブルの特定チャンク範囲のみを処理
        
        Args:
            table_name: 処理するテーブル名
            chunk_size: 処理する行数
            offset: 開始行オフセット
            output_file: Parquet出力ファイルパス（Noneの場合は出力なし）
            
        Returns:
            処理結果
        """
        # テーブルの存在確認
        if not check_table_exists(self.conn, table_name):
            raise ValueError(f"Table {table_name} does not exist")
        
        # テーブル情報の取得
        columns = get_table_info(self.conn, table_name)
        if not columns:
            raise ValueError(f"No columns found in table {table_name}")
        
        # LIMIT/OFFSETで指定された範囲のデータのみを取得
        print(f"チャンク処理: {table_name}テーブルの{offset+1}～{offset+chunk_size}行目を処理")
        
        # バイナリデータの取得
        start_time = time.time()
        buffer_data, buffer = get_binary_data(
            self.conn, 
            table_name, 
            limit=chunk_size, 
            offset=offset
        )
        
        # 出力ハンドラの設定（チャンク専用のParquet出力）
        chunk_output_handler = OutputHandler(output_file)
        
        # チャンク処理用の変数初期化
        num_columns = len(columns)
        processed_rows = 0
        
        # GPUバッファの初期化
        buffers = self.memory_manager.initialize_device_buffers(columns, chunk_size)
        
        try:
            # バイナリデータの解析
            chunk_array, field_offsets, field_lengths, rows_in_chunk = self.parser.parse_chunk(
                buffer_data, 
                max_chunk_size=1024*1024,
                num_columns=num_columns,
                start_row=0,  # バッファ内の開始行（すでにオフセット済み）
                max_rows=chunk_size
            )
            
            if rows_in_chunk == 0:
                print("処理する行がありません")
                return None
                
            print(f"処理中: {rows_in_chunk}行")
            
            # GPUで解析
            d_col_types = None
            d_col_lengths = None
            
            try:
                print(f"GPUデコード開始: チャンク（{rows_in_chunk}行）")
                
                # チャンク間でのGCを促進
                import gc
                gc.collect()
                from numba import cuda
                cuda.synchronize()
                
                # デバイスに転送
                d_col_types = self.memory_manager.transfer_to_device(buffers["col_types"], np.int32)
                d_col_lengths = self.memory_manager.transfer_to_device(buffers["col_lengths"], np.int32)
                
                # デコード処理
                chunk_results = self.gpu_decoder.decode_chunk(
                    buffers, 
                    chunk_array, 
                    field_offsets, 
                    field_lengths, 
                    rows_in_chunk, 
                    columns
                )
                
                print(f"GPUデコード完了")
                
                # 結果の処理
                chunk_output_handler.process_chunk_result(chunk_results)
                final_results = chunk_output_handler.print_summary()
                
                # 処理済み行数の更新
                processed_rows = rows_in_chunk
                
            except Exception as e:
                print(f"チャンク処理中にエラー発生: {e}")
                raise
                
            finally:
                # GPUリソースのクリーンアップ
                cleanup_dict = {
                    "d_col_types": d_col_types, 
                    "d_col_lengths": d_col_lengths
                }
                self.memory_manager.cleanup_buffers(cleanup_dict)
                self.memory_manager.cleanup_buffers(buffers)
                
                # 明示的にGPU同期を取り、全リソースが解放されたことを確認
                from numba import cuda
                cuda.synchronize()
                
                # ガベージコレクションを促進
                import gc
                gc.collect()
        
            print(f"チャンク処理完了: {processed_rows}行")
            print(f"処理時間: {time.time() - start_time:.3f}秒")
            
            return final_results
            
        except Exception as e:
            print(f"チャンク処理でエラー: {e}")
            raise
    
    def _process_data_in_chunks(self, buffer_data, columns, total_rows, output_file=None):
        """データをチャンクに分けて処理する共通ロジック
        
        Args:
            buffer_data: PostgreSQLから取得したバイナリデータ
            columns: カラム情報のリスト
            total_rows: 処理する合計行数
            output_file: Parquet出力ファイルパス
            
        Returns:
            処理結果の辞書
        """
        # 最適なチャンクサイズを計算
        num_columns = len(columns)
        optimal_chunk_size = self.memory_manager.calculate_optimal_chunk_size(columns, total_rows)
        
        # チャンク設定の初期化
        chunk_config = ChunkConfig(total_rows, optimal_chunk_size)
        print(f"GPUメモリに基づく最適チャンクサイズ: {chunk_config.rows_per_chunk}行")
        
        # 出力ハンドラの設定
        output_handler = OutputHandler(output_file)
        
        # 複数チャンク処理のための変数初期化
        all_results = {}  # 全チャンクの結果を集約する辞書
        processed_rows = 0
        chunk_count = 0
        
        # 全行を複数チャンクに分けて処理
        while processed_rows < total_rows:
            # 現在のチャンクサイズを計算
            current_chunk_size = min(chunk_config.rows_per_chunk, total_rows - processed_rows)
            chunk_count += 1
            
            print(f"\n== チャンク {chunk_count}: {processed_rows+1}～{processed_rows+current_chunk_size}行目を処理 (全{total_rows}行中) ==")
            
            buffers = self.memory_manager.initialize_device_buffers(columns, current_chunk_size)
            from numba import cuda
            import cupy as cp

            # TODO: GPU parsing integration needed
            raise NotImplementedError("GPU parsing logic needs to be integrated in _process_data_in_chunks.")
            
            rows_in_chunk = current_chunk_size
            chunk_array = None
            field_offsets = None
            field_lengths = None

            # Check if rows exist *before* the try block
            if rows_in_chunk == 0:
                print("これ以上処理する行がありません")
                break

            print(f"処理中: {rows_in_chunk}行")

            # Initialize d_col_types and d_col_lengths before try block
            d_col_types = None
            d_col_lengths = None
            try:
                # GPUデコード処理
                d_col_types = self.memory_manager.transfer_to_device(buffers["col_types"], np.int32)
                d_col_lengths = self.memory_manager.transfer_to_device(buffers["col_lengths"], np.int32)

                chunk_results = self.gpu_decoder.decode_chunk(
                    buffers,
                    chunk_array,
                    field_offsets,
                    field_lengths,
                    rows_in_chunk,
                    columns
                )
                
                # Parquet出力処理
                if output_file:
                    output_handler.process_chunk_result(chunk_results)
                
                # 結果の集約
                if not all_results:
                    # 初回は結果をそのまま使用
                    all_results = chunk_results
                else:
                    # 2回目以降は結果を連結
                    for col_name, data in chunk_results.items():
                        if isinstance(data, np.ndarray):
                            all_results[col_name] = np.concatenate([all_results[col_name], data])
                        elif isinstance(data, list):
                            all_results[col_name].extend(data)
                
                # 処理済み行数を更新
                processed_rows += rows_in_chunk
                
            except Exception as e:
                print(f"チャンク {chunk_count} の処理中にエラー: {e}")
                import traceback
                traceback.print_exc()
            finally:
                # GPUリソースのクリーンアップ
                cleanup_dict = {
                    "d_col_types": d_col_types,
                    "d_col_lengths": d_col_lengths,
                }
                self.memory_manager.cleanup_buffers(cleanup_dict)
                self.memory_manager.cleanup_buffers(buffers)
        
        # 出力ハンドラのサマリー表示
        if output_file:
            output_handler.print_summary()
        
        return all_results

    def process_table(self, table_name: str, limit: Optional[int] = None):
        """テーブル全体を処理（複数チャンク対応）"""
        # テーブルの存在確認
        if not check_table_exists(self.conn, table_name):
            raise ValueError(f"Table {table_name} does not exist")

        # テーブル情報と行数の取得
        columns = get_table_info(self.conn, table_name)
        if not columns:
            raise ValueError(f"No columns found in table {table_name}")

        total_rows = min(get_table_row_count(self.conn, table_name), limit if limit else float('inf'))

        # バイナリデータの取得
        start_time = time.time()
        buffer_data, buffer = get_binary_data(self.conn, table_name, limit)

        # 共通のチャンク処理ロジックを呼び出し
        return self._process_data_in_chunks(buffer_data, columns, total_rows, self.parquet_output)
            
    def process_query(self, query: str):
        """SQLクエリを実行し結果を処理する
        
        Args:
            query: 実行するSQLクエリ
            
        Returns:
            pandas.DataFrame: 処理結果のデータフレーム
        """
        # テーブル情報と行数の取得
        buffer_data, buffer = get_binary_data(
            self.conn,
            table_name="", 
            query=query
        )
        
        # バッファが空の場合
        if len(buffer_data) == 0:
            print("クエリ結果が空です")
            return None
        
        # カラム情報を最初の行から推定（簡易版）
        import pandas as pd
        
        try:
            parser = BinaryDataParser(use_gpu=False)
            chunk_array, field_offsets, field_lengths, rows_in_chunk = parser.parse_chunk(
                buffer_data, max_chunk_size=len(buffer_data), max_rows=10000
            )
            
            if rows_in_chunk == 0:
                print("有効な行がありません")
                return None
                
            field_count = 0
            for i in range(min(100, len(field_lengths))):
                if field_lengths[i] != 0:
                    field_count += 1
                else:
                    break
                        
            num_cols = field_count if rows_in_chunk > 0 else 0
            
            if num_cols == 0:
                print("列情報を取得できませんでした")
                return None
                
            columns = get_query_column_info(self.conn, query)
            
            if not columns:
                print("PostgreSQLメタデータからカラム情報を取得できませんでした。デフォルト設定を使用します。")
                columns = []
                for i in range(num_cols):
                    columns.append(ColumnMeta(f"col_{i}", 0, -1, UNKNOWN, 0, 1, -1))

            # GPUバッファの初期化
            buffers = self.memory_manager.initialize_device_buffers(columns, rows_in_chunk)
            
            # GPUでデコード
            try:
                chunk_results = self.gpu_decoder.decode_chunk(
                    buffers,
                    chunk_array,
                    field_offsets,
                    field_lengths,
                    rows_in_chunk,
                    columns
                )
                
                # 結果をDataFrameに変換
                df = pd.DataFrame(chunk_results)
                # カラム名をインデックス順に設定
                df.columns = [f"col_{i}" for i in range(len(df.columns))]
                
                return df
                
            except Exception as e:
                print(f"GPUデコード中にエラー: {e}")
                return None
                
        except Exception as e:
            print(f"クエリ処理中にエラー: {e}")
            return None
            
    def process_custom_query(self, query: str, output_file: Optional[str] = None):
        """カスタムSQLクエリを実行し、結果をGPUで処理してParquetファイルに出力する

        Args:
            query: 実行するSQLクエリ
            output_file: Parquet出力ファイルパス（Noneの場合は出力なし）

        Returns:
            処理結果（dictまたはDataFrame）
        """
        print(f"カスタムSQLクエリの実行: {query}")
        start_time = time.time()

        # テーブル情報と行数の取得
        buffer_data, buffer = get_binary_data(
            self.conn,
            table_name="",
            query=query
        )

        # バッファが空の場合
        if len(buffer_data) == 0:
            print("クエリ結果が空です")
            return None

        # まずCPUで簡易パースして列数とタイプを推定（少数行のみ）
        try:
            parser = BinaryDataParser(use_gpu=False)
            chunk_array, field_offsets, field_lengths, rows_in_sample = parser.parse_chunk(
                buffer_data,
                max_chunk_size=len(buffer_data),
                max_rows=100  # メタデータ推定用に少数行のみ処理（10000→100に変更）
            )

            if rows_in_sample == 0:
                print("有効な行がありません")
                return None

            # 最初の行から列数を推定
            if rows_in_sample > 0:
                # フィールド数をカウント
                field_count = 0
                for i in range(min(100, len(field_lengths))):
                    if field_lengths[i] != 0:
                        field_count += 1
                    else:
                        break

                # 先頭行のフィールド数から推定
                num_cols = field_count
            else:
                num_cols = 0

            if num_cols == 0:
                print("列情報を取得できませんでした")
                return None

            # PostgreSQLメタデータAPIを使用して正確なカラム型情報を取得
            columns = get_query_column_info(self.conn, query)

            # カラム情報が取得できない場合はフォールバック
            if not columns:
                print("PostgreSQLメタデータからカラム情報を取得できませんでした。デフォルト設定を使用します。")
                # デフォルトのカラム情報を作成（すべて文字列型と仮定）
                columns = []
                for i in range(num_cols):
                     # Use ColumnMeta here
                    columns.append(ColumnMeta(f"col_{i}", 0, -1, UNKNOWN, 0, 1, -1)) # Provide default values for ColumnMeta fields
                    # Example: ColumnMeta(name, pg_oid, typmod, arrow_id, elem_size, is_variable, var_index)
                    # Adjust default values as needed based on ColumnMeta definition

            # --- 以下が変更部分 ---
            # 全行数を取得または推定
            # SQLからの正確な行数が取得できない場合は、バッファサイズから概算
            try:
                # ROW_NUMBERを使った行数取得クエリ
                count_query = f"SELECT COUNT(*) FROM ({query}) AS subquery"
                count_cursor = self.conn.cursor()
                count_cursor.execute(count_query)
                total_rows = count_cursor.fetchone()[0]
                count_cursor.close()
            except Exception as e:
                print(f"行数の正確な取得に失敗: {e}")
                # バッファサイズとサンプルから行数を概算
                avg_row_size = len(buffer_data) / rows_in_sample if rows_in_sample > 0 else 1000
                total_rows = int(len(buffer_data) / avg_row_size) + 1
                print(f"概算行数: {total_rows}行")

            print(f"クエリ結果: 推定{total_rows}行")

            # 共通のチャンク処理ロジックを呼び出し
            return self._process_data_in_chunks(buffer_data, columns, total_rows, output_file)

        except Exception as e:
            print(f"クエリ処理中にエラー: {e}")
            import traceback
            traceback.print_exc()
            return None
    
    def close(self):
        """リソースの解放"""
        if hasattr(self, 'conn') and self.conn:
            self.conn.close()

def load_table_optimized(table_name: str, limit: Optional[int] = None, parquet_output: Optional[str] = None):
    """最適化されたGPU実装でテーブルを読み込む（コンビニエンス関数）"""
    processor = PgGpuProcessor(parquet_output=parquet_output)
    try:
        results = processor.process_table(table_name, limit)
        
        # Parquet出力が指定されている場合の検証
        if parquet_output:
            print(f"\nParquetファイルが保存されました: {parquet_output}")
            print("cuDFで読み込みテスト:")
            try:
                import cudf
                df = cudf.read_parquet(parquet_output)
                print("\n最初の5行:")
                print(df.head(5))
                print("\n最後の5行:")
                print(df.tail(5))
            except ImportError:
                print("cuDFがインストールされていないため、読み込みテストをスキップします")
            except Exception as e:
                print(f"Parquetファイル読み込み中にエラー: {e}")
                
        return results
    finally:
        processor.close()

# Import os for environment variable access
import os

if __name__ == "__main__":
    import argparse
    import time

    # Import necessary classes if not already imported at top level
    # from .pg_connector import connect_to_postgres # Already imported
    # from .gpu_memory_manager_v2 import GPUMemoryManagerV2 # Already imported
    # from .gpu_decoder_v2 import GPUDecoderV2 as GPUDecoder # Already imported
    # from .output_handler import OutputHandler # Already imported
    # from .meta_fetch import ColumnMeta # Already imported

    parser = argparse.ArgumentParser(description='PostgreSQL GPU Parser')
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--table', help='Table name to process')
    group.add_argument('--sql', help='SQL query to process')
    parser.add_argument('--limit', type=int, default=None, help='Limit number of rows (used with --table)')
    parser.add_argument('--parquet', help='Output path for Parquet file')
    # Add arguments for DB connection if not using environment variable exclusively
    # parser.add_argument('--dbname', default='postgres')
    # parser.add_argument('--user', default='postgres')
    # parser.add_argument('--password', default='postgres')
    # parser.add_argument('--host', default='localhost')
    args = parser.parse_args()

    start_time = time.time()
    processor = None

    try:
        # Instantiate PgGpuProcessor, passing parquet path
        # DB connection details could be passed here if args are added,
        # otherwise it uses defaults or environment variables (as implemented in __init__)
        processor = PgGpuProcessor(
            # dbname=args.dbname, user=args.user, password=args.password, host=args.host, # If args added
            parquet_output=args.parquet
        )

        if args.sql:
            # Process custom SQL query
            print(f"=== SQLクエリ処理 ===")
            print(f"SQL: {args.sql}")
            print("\n[最適化GPU実装]")
            # Call process_custom_query directly
            results = processor.process_custom_query(args.sql, args.parquet) # Pass parquet path again if needed by method
        elif args.table:
            # Process table (using the processor instance directly is cleaner)
            print(f"=== {args.table}テーブル処理 ===")
            print("\n[最適化GPU実装]")
            # Call process_table directly on the created processor instance
            results = processor.process_table(args.table, args.limit)
            # Note: load_table_optimized creates its own processor, which is redundant here.
            # results = load_table_optimized(args.table, args.limit, args.parquet) # Keep if preferred
        else:
            # Should not be reached due to mutually_exclusive_group
            print("Error: --table または --sql のいずれかを指定してください。")
            exit(1)

        gpu_time = time.time() - start_time
        print(f"処理時間: {gpu_time:.3f}秒")

        # Optional: Print or process results if needed
        # print("Results:", results)

    except Exception as e:
        print(f"処理中にエラーが発生しました: {e}")
        import traceback
        traceback.print_exc()
    finally:
        # Ensure processor and its connection are closed
        if processor:
            processor.close()
