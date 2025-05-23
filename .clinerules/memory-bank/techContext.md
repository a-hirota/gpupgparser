# 技術コンテキスト

## 使用技術

### 主要言語とライブラリ

- **Python**: 主要な開発言語
- **CUDA**: NVIDIAのGPU向け並列計算プラットフォーム
- **Numba**: JITコンパイルによるPythonコードの高速化 (`>=0.57` 推奨、128ビット演算のため)
- **CuPy**: CUDA対応のNumPy互換ライブラリ
- **PyArrow**: Apache Arrowの実装（メモリ内列指向データ構造） (`>=15.0` 推奨、`pack_bits`のため)
- **psycopg**: PostgreSQL接続用Pythonドライバ (v3)
- *(関連技術)* pandas/cuDF: 生成されたArrowデータの後続処理で使用される可能性

### GPU処理関連

- **CUDA Toolkit**: カーネル開発とGPU操作
- **CUDA Streams**: 非同期実行と並列処理
- **Thrust**: CUDA向け並列アルゴリズムライブラリ
- **NVTX**: NVIDIAのトレーシングツールキット（プロファイリング用）

### データフォーマット

- **PostgreSQL Binary Format**: COPY TO STDOUT (FORMAT BINARY)の出力形式
- **Apache Arrow**: メモリ内列指向フォーマット (Decimal128含む)
- **Parquet**: 列指向の永続化データフォーマット

## 開発環境

### ハードウェア要件

- **GPU**: CUDA対応のNVIDIA GPU
  - 開発環境: NVIDIA GeForce RTX 3090 (24GB VRAM)
  - テスト環境: NVIDIA A100 (40GB/80GB VRAM)
- **CPU**: マルチコアプロセッサー
  - 最小: 4コア
  - 推奨: 8コア以上
- **メモリ**:
  - 最小: 16GB RAM
  - 推奨: 32GB以上RAM
- **ストレージ**: SSD推奨（特に大規模データセット処理時）

### ソフトウェア環境

- **OS**:
  - Linux (Ubuntu 20.04/22.04)
  - Windows 10/11 (WSL2サポート)
- **CUDA**: 11.8以上 (Numbaでの128ビット演算サポートのため)
- **Python**: 3.8以上
- **PostgreSQL**: 12以上
- **Numba**: `>=0.57` 推奨
- **PyArrow**: `>=15.0` 推奨

### 依存ライブラリバージョン (推奨含む)

```
numba>=0.57.0 # 128-bit演算サポートのため推奨
numpy>=1.20.0
cupy-cuda11x>=11.0.0 # CUDAバージョンに合わせて調整
psycopg>=3.0.0 # psycopg (v3) を使用
pyarrow>=15.0.0 # pyarrow.compute.pack_bits 使用のため推奨
# pandas は直接依存しない
```

### テスト環境

- **E2Eテスト実行:**
    - 環境変数 `GPUPASER_PG_DSN` に接続DSN文字列を設定する必要がある。
    - 例: `export GPUPASER_PG_DSN='dbname=postgres user=postgres host=localhost port=5432'`
- **インポートパス:** プロジェクトルートから `pytest` を実行する場合、`test/` ディレクトリ内のテストファイルは `src.` から始まる絶対パスでモジュールをインポートする必要がある。

## 技術的制約

### GPU処理の制約

1. **CUDA制限**:
   - 最大グリッド/ブロックサイズ、共有メモリ、レジスタ制限など。

2. **GPUメモリ制約**:
   - VRAM容量、PCIe帯域幅。

3. **型変換の問題**:
   - **NUMERIC型:** **解決済み。** Numbaカーネル内で `uint64` ペアを用いて128ビット演算を実装し、Arrow Decimal128形式 (16バイトLE整数) に変換。ただし、実装した128ビット演算ヘルパー（特に乗除算）の複雑性とエッジケースでの正確性には注意が必要。
   - **未対応型:** TIME, INTERVAL, UUIDなどへの対応は未実装。
   - **エンディアン変換:** 固定長型でのBE→LE変換は実装済み。

### PostgreSQL関連の制約

1. **バイナリフォーマットの制約**:
   - ネットワークバイトオーダー（ビッグエンディアン）。
   - 特殊エスケープシーケンス（現状未対応）。

2. **接続制約**:
   - セッション制限、ネットワーク帯域、サーバーリソース。

### マルチGPU対応の課題

1. **コンテキスト管理**: プロセスベース並列化を採用。
2. **ロードバランシング**: 未実装。
3. **結果統合**: 未実装（現在は個別ファイル出力）。

## パフォーマンス特性

### ボトルネック分析

1. **I/Oボトルネック**: DB取得速度、ディスク書き込み速度。
2. **メモリ転送ボトルネック**: CPU-GPU間転送。
3. **計算ボトルネック**: カーネル実行効率、特に複雑な型変換（Decimal128など）や可変長処理。

### スケーリング特性

1. **垂直スケーリング**: GPU性能・メモリ向上による効果期待。
2. **水平スケーリング**: マルチGPU（プロセスベース）で対応。

### 最適化ポイント

1. **メモリ管理**:
   - **可変長列:** 二段階処理（Prefix Sum + 再確保）によるメモリ効率化は**実装済**。
   - バッファ再利用。
   - 非同期メモリ転送とカーネル実行のオーバーラップ（パイプライン化）。
   - `from_buffers` 導入によるCPU側メモリコピー削減（実装済、`pyarrow.cuda` 利用可能時）。

2. **カーネル最適化**:
   - スレッド負荷分散、メモリアクセスパターン、ワープ効率。
   - 128ビット演算ヘルパーの最適化（可能であれば）。
   - CPUでのBooleanパック処理の最適化（GPU化など）。

3. **データレイアウト**:
   - キャッシュ効率の良いメモリ配置。

## 将来的な技術的展望

### 短期的な技術拡張 (現在の課題)

1.  **DECIMAL型精度警告の調査・修正:** `src/meta_fetch.py` での精度・スケール情報取得を修正する。
2.  **基本データ型の検証と改善:**
    *   FLOAT, DATE, TIMESTAMP の `from_buffers` 利用をE2Eテストで詳細に検証。
    *   Timestamp型のタイムゾーン対応 (`meta_fetch.py` での情報取得含む)。
    *   Boolean型のCPUパック処理のパフォーマンス評価と、必要に応じたGPU化検討。
    *   Stride違いによる固定長型のホストコピーフォールバック解消検討。
3.  **エラーハンドリング強化:** カーネル内エラー検知、メモリ確保失敗時の対応。
4.  **Pass 1 GPUカーネルの検証:** `pass1_len_null` の詳細検証。
5.  **パフォーマンスチューニング:** Grid size警告への対応、プロファイリングに基づくボトルネック解消。


### 中期的な技術拡張

1.  **処理パイプラインの非同期化:** CUDA Streamsを用いたデータ転送とカーネル実行のオーバーラップ。
2.  **追加データタイプのサポート:** TIME, INTERVAL, UUID, 配列型などへの対応拡張。
3.  **マルチGPU処理の安定化:** ロードバランシング、エラー回復、結果統合。

### 長期的な技術展望

1. **分散処理フレームワークとの統合**: Dask/Rapids連携。
2. **GPUDirect技術の活用**: GPUDirect Storage/RDMA。
