# BOOTH Asset Manager

BOOTHで購入したVRChatアバター素材(zip/unitypackage/vrm/fbx等)を個人管理するWebツール。
実ファイルはGoogle Drive、メタデータはSQLite(実体もGoogle Drive上に保存)で管理する。

設計方針の詳細は `docs/` (実装が進み次第追記) を参照。BOOTH側の購入・ダウンロード操作は自動化しない — 取り込みは常にユーザーの手動ダウンロードを起点とする。

## 開発環境セットアップ

```bash
uv sync
cp .env.example .env
uv run uvicorn app.main:app --reload
```

`http://localhost:8000/healthz` が `{"status": "ok"}` を返せば起動確認OK。

ローカルにデータベースファイル(`data/app.db`)が存在しない状態でアクセスすると `/setup` にリダイレクトされ、Google Driveへの接続を求められる(下記「Google OAuth設定」参照)。開発中にDrive接続なしでUIだけ触りたい場合は、先に `uv run alembic upgrade head` を実行してローカルDBだけ作成しておくと `/setup` を経由せずに動作する(Driveには未接続の状態として動く)。

## Google OAuth設定

1. [Google Cloud Console](https://console.cloud.google.com/) でプロジェクトを作成し、「Google Drive API」を有効化する。
2. 「APIとサービス」→「認証情報」で OAuth クライアントID(種類: ウェブアプリケーション)を作成する。
   - 承認済みのリダイレクトURI に `http://localhost:8000/oauth/google/callback` (本番では実際のURL)を追加する。
3. 発行された クライアントID/シークレット を `.env` の `GOOGLE_OAUTH_CLIENT_ID` / `GOOGLE_OAUTH_CLIENT_SECRET` に設定する。
4. `TOKEN_ENCRYPTION_KEY` と `SESSION_SECRET_KEY` を生成して設定する(`.env.example` にコマンド例あり)。
5. アプリにアクセスすると `/setup` が表示されるので、Googleでログインして認可する。初回はDriveに新規データベースが作成される。ボリューム消失後の復旧など、既存のDriveデータベースを使う場合は先に `DRIVE_DB_FILE_ID` を設定してから認可する。

## テスト

```bash
uv run pytest
```

## Docker

```bash
docker build -t booth-asset-manager .
docker run --rm -p 8000:8000 --env-file .env -v $(pwd)/data:/data booth-asset-manager
```

コンテナは単一プロセス・単一ワーカー(`--workers 1`)で動作する前提。ローカルSQLiteキャッシュとアップロード一時領域は `DATA_DIR`(既定 `/data`)配下に置かれる。

## Pterodactyl

- Egg設定で `DATA_DIR` を永続ボリュームにマッピングすること。
- 環境変数は `.env.example` を参照。`DRIVE_DB_FILE_ID` はディザスタリカバリ用(ボリュームを失った場合に指定するとDriveからDBを復元する)。

## セキュリティ上の注意

- Google OAuthトークンはSQLite内に暗号化して保存する(`TOKEN_ENCRYPTION_KEY`)。最終的なセキュリティはホスト・Googleアカウント自体の保護に依存する、個人ツールとしての現実的な妥協点。
- `.env` は絶対にコミットしないこと(`.gitignore`済み)。
- 公開URLで動かす場合は `APP_LOGIN_PASSWORD` によるログインゲートの設定を推奨(Phase 6で追加予定)。
