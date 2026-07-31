# BOOTH Asset Manager

BOOTHで購入したVRChatアバター素材(zip/unitypackage/vrm/fbx等)を個人管理するWebツール。
実ファイルはGoogle Drive、メタデータはSQLite(実体もGoogle Drive上に保存)で管理する。

BOOTH側の購入・ダウンロード操作は自動化しない — 取り込みは常にユーザーの手動ダウンロードを起点とする。

## 主な機能

- 商品登録(ドラッグ&ドロップ、拡張子/サイズ/マジックバイト検証、Drive自動アップロード)
- サムネイル自動取得(商品ページのog:image。失敗時は手動アップロードにフォールバック)
- タグ・対応アバターの自由入力+自動マスタ登録
- キーワード検索・タグ/アバター/ショップ/ステータス絞り込み(カード/テーブル表示切替)
- 詳細閲覧・メタデータ編集・更新チェック履歴
- Drive経由でのファイルダウンロード(通常のブラウザダウンロードとして)
- Google Drive⇔ローカルSQLiteの自動同期(デバウンス書き戻し、シャットダウン時フラッシュ、リモート競合検知)

## 開発環境セットアップ

```bash
uv sync
cp .env.example .env
uv run uvicorn app.main:app --reload
```

`http://localhost:8000/healthz` が `{"status": "ok"}` を返せば起動確認OK。

ローカルにデータベースファイル(`data/app.db`)が存在しない状態でアクセスすると `/setup` にリダイレクトされ、Google Driveへの接続を求められる(下記「Google OAuth設定」参照)。開発中にDrive接続なしでUIだけ触りたい場合は、先に `uv run alembic upgrade head` を実行してローカルDBだけ作成しておくと `/setup` を経由せずに動作する(Driveには未接続の状態として動く。取り込み・ダウンロード・サムネイル表示など実際にDriveへアクセスする機能は「Google Driveが未接続です」というエラーになる)。

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

Google Driveへの実アクセスは `DriveClient` インターフェース(`app/drive/client.py`)の背後に隠蔽されており、テストは全て `FakeDriveClient`(インメモリ実装)を使う。実Driveとの疎通確認は自動テスト対象外(手動でGoogle Cloudの認証情報を用意した上で `/setup` から実施)。

## Docker

```bash
docker build -t booth-asset-manager .
docker run --rm -p 8000:8000 --env-file .env -v $(pwd)/data:/data booth-asset-manager
```

コンテナは単一プロセス・単一ワーカー(`--workers 1`)で動作する前提(SQLite⇔Drive同期がシングルライターを仮定しているため)。ローカルSQLiteキャッシュとアップロード一時領域は `DATA_DIR`(既定 `/data`)配下に置かれる。

## Pterodactyl

推奨は「Dockerイメージを起動するだけ」のEgg(このリポジトリのDockerfileを指定、または汎用Docker Image Egg)。

- Egg設定で `DATA_DIR`(既定 `/data`)を永続ボリュームにマッピングすること。
- 環境変数は `.env.example` を参照し、Eggの変数(Variables)として設定する。特に以下は必須:
  - `GOOGLE_OAUTH_CLIENT_ID` / `GOOGLE_OAUTH_CLIENT_SECRET` / `GOOGLE_OAUTH_REDIRECT_URI`(公開URLに合わせて設定し、Google Cloud Console側のリダイレクトURIにも追加する)
  - `TOKEN_ENCRYPTION_KEY` / `SESSION_SECRET_KEY`
- `DRIVE_DB_FILE_ID` はディザスタリカバリ用(ボリュームを失った場合に指定するとDriveからDBを復元する)。`/settings` 画面に表示されるIDを控えておくと良い。
- インターネットに公開する場合は `APP_LOGIN_PASSWORD` を必ず設定すること(下記参照)。
- `PORT` はEggがコンテナに割り当てるポートに合わせる。

### 「Generic Python」系Egg(parkervcp/yolks:python_3.13など)を使う場合

Dockerfileを使わず、リポジトリをgit cloneして `pip install -r requirements.txt` → `python main.py` を実行するタイプのEggでも動くように、リポジトリ直下に `requirements.txt`(`uv export`で生成)と `main.py`(uvicornを単一ワーカーで起動するだけの薄いエントリポイント)を用意してある。Eggの変数で以下を設定する:

- `PY_FILE` = `main.py`
- `REQUIREMENTS_FILE` = `requirements.txt`
- Egg側の起動コマンドが `pip install --prefix .local` を使う場合、それが自動でパスに反映されない構成もあるので起動ログでimportエラーが出ないか確認すること。
- ポートはEggが `SERVER_PORT` として渡す想定(`main.py` はそれを最優先で読む。無ければ `PORT`、それも無ければ8000)。
- `requirements.txt` は `uv.lock` から生成しているため、依存関係を更新したら `uv export --no-hashes --no-dev --no-emit-project -o requirements.txt` で再生成すること。

このEggでは `uv` や単一ワーカーの強制がDockerfileほど厳密に保証されないため、可能であればDockerベースのEgg運用を推奨する。

## セキュリティ

- **OAuthトークン**: SQLite内に `TOKEN_ENCRYPTION_KEY`(Fernet)で暗号化して保存する。ログには一切出力しない(`app/logging_conf.py` のredactionフィルタでも二重に保護)。最終的なセキュリティはホスト・Googleアカウント自体の保護に依存する、個人ツールとしての現実的な妥協点。
- **アップロード検証**: 拡張子allowlist・サイズ上限・マジックバイト検証を行う。zipの中身は一切展開・列挙しない(zip爆弾・パストラバーサル対策)。
- **CSRF対策**: セッションに紐づくトークンを発行し、HTMXリクエストは `X-CSRF-Token` ヘッダー、通常フォーム送信は隠しフィールドで検証する(状態変更を伴う全POST/DELETEに適用)。
- **ログインゲート**: `APP_LOGIN_PASSWORD` を設定すると、初回セットアップ(`/setup`・`/oauth/*`)以外の全ルートがパスワード保護される。個人利用でPterodactyl等インターネットに公開する場合は設定を強く推奨。
- **整合性チェック**: `/settings` 画面から、DB上のファイル参照が実際にDrive上に存在するかを確認できる(取り込み失敗時の補償削除がさらに失敗した場合などを検出する保険)。
- `.env` は絶対にコミットしないこと(`.gitignore`済み)。

## アーキテクチャ

- `app/models/` - SQLAlchemy 2.0モデル
- `app/schemas/` - Pydantic DTO
- `app/services/` - ビジネスロジック(DB・Driveへのアクセスはここに集約)
- `app/drive/` - `DriveClient` インターフェースと実装(`GoogleDriveClient` / `FakeDriveClient`)
- `app/web/pages/` - フルページ(HTML)ルート
- `app/web/fragments/` - HTMX向け部分HTMLルート
- `app/api/routers/` - JSON/バイナリ API(ダウンロード、OAuthコールバック)
- `alembic/` - DBマイグレーション

ルーターはサービス層のみを呼び出し、ORMクエリやDrive呼び出しを直接書かない方針。
