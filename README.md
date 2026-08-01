# VRC Content Manager

BOOTHで購入したVRChatアバター素材(zip/unitypackage/vrm/fbx等)を個人管理するWebツール。
実ファイルはGoogle Drive、メタデータはSQLite(実体もGoogle Drive上に保存)で管理する。

BOOTH側の購入・ダウンロード操作は自動化しない — 取り込みは常にユーザーの手動ダウンロードを起点とする。

## 画面構成

- **TOP**(`/items`) - ファイル管理のメイン画面。ファイル一覧(カード/テーブル切替)・検索/絞り込み・アップロード・詳細表示/編集/削除をすべてここで行う
  - Google Driveライクなアップロード: ページ上部の「アップロード」ボタン、またはページ全体へのドラッグ&ドロップでファイルをすぐにアップロード・仮登録し、詳細(商品名・ショップ・タグ等)は右側の詳細パネル/編集画面で後から入力する
  - カード/行をクリックすると右側の詳細パネルにその場で詳細を表示(ページ遷移なしのマスタ・ディテール表示。URLは連動して更新されるので直接リンク/リロードも可能)。編集・ダウンロード・削除もここから
  - **アバターリスト**(`/avatars`) - 登録済みの対応アバターを一覧表示し、クリックでそのアバターに絞り込んだTOP画面へ
- **設定**(`/settings`) - Google Drive連携・**Driveと同期**・整合性チェック・**ショップ管理**・OAuthクライアント・ログインパスワード・動作設定をまとめた画面

## 主な機能

- 編集画面でBOOTHの商品ページURLを貼り付けると、商品名・ショップ名・価格・説明文・サムネイルを自動取得(取得後も修正可能。失敗時は手動入力にフォールバック)。タグ・対応アバターは登録済みの中から一致するものを候補チップとして提案
- タグ・対応アバターの自由入力+自動マスタ登録
- キーワード検索・タグ/アバター/ショップ/ステータス絞り込み(カード/テーブル表示切替)
- 複数選択によるチェックボックス一括操作(一括削除・ステータス/タグ/対応アバター/お気に入りの一括編集)
- メタデータ編集・更新チェック履歴・削除(Drive上のファイルも削除)
- Drive経由でのファイルダウンロード(通常のブラウザダウンロードとして)
- Google Drive⇔ローカルSQLiteの自動同期(デバウンス書き戻し、シャットダウン時フラッシュ、リモート競合検知)
- **Drive上で直接行った変更の取り込み**(設定画面の「Driveと同期」): 実ファイルを保存する`VRC-ContentManager`フォルダを対象に、(1) 手動で削除・移動して見つからなくなったファイルの参照をDB側から削除(商品自体やタグ等のメタデータは保持)、(2) フォルダ内に直接追加したファイルを新しい商品として取り込み(ショップは「未設定」で登録、詳細はあとから編集)を行う。フォルダ自体が削除されていた場合も次回アクセス時に自動再作成される。ユーザー操作で明示的に実行する方式(バックグラウンドでの自動実行はしない)。

現時点で未実装(既知の今後の課題): アップロード時の同名ファイル確認ダイアログ、同一販売URL・別対応アバターの商品をまとめて表示するグルーピング。

## UI

サーバーサイドレンダリング(Jinja2 + HTMX、ビルドステップなし)。ヘッダー(サービス名・表示モード切替・設定・ユーザーメニュー)+ 左サイドバー(一覧/アバターリスト)+ コンテンツの構成。見た目は細めのフォント(Noto Sans JP Light)と半透明・強めのぼかし(Apple風「Liquid Glass」寄りのglassmorphism)を基調にした、Google Fonts + Google Icons(Material Symbols Outlined)使用のUI。

- CSSフレームワークは [Tailwind CSS](https://tailwindcss.com/)(CDN版、ビルド不要)。共通コンポーネント(ボタン・フォーム項目・カード・アラート等)は `app/templates/_macros.html` のJinjaマクロにまとめて重複を防いでいる。
- 表示モードは端末設定に追従(デフォルト)・ライト・ダークをヘッダーから切替可能(`localStorage`に保存)。
- アクセシビリティはWCAG 2.1 AAのコントラスト比(文字4.5:1・UI部品3:1)を全配色ペアで検証済み。全フォーム項目に`<label for>`、装飾アイコンに`aria-hidden`、フォーカスリング、44px以上のタップ領域、スキップリンクを実装。
- ファイルアップロードはドラッグ&ドロップに対応(ネイティブの`<input type="file">`は常に表示・操作可能なままにしてあり、D&Dは拡張として動作するため、キーボード/スクリーンリーダー操作を損なわない)。

## 開発環境セットアップ

```bash
uv sync
cp .env.example .env
uv run uvicorn app.main:app --reload
```

`http://localhost:8000/healthz` が `{"status": "ok"}` を返せば起動確認OK。

ローカルにデータベースファイル(`data/app.db`)が存在しない状態でアクセスすると `/setup` にリダイレクトされ、Google OAuthクライアントの登録とDrive接続を求められる(下記「Google OAuth設定」参照)。開発中にDrive接続なしでUIだけ触りたい場合は、先に `uv run alembic upgrade head` を実行してローカルDBだけ作成しておくと `/setup` を経由せずに動作する(Driveには未接続の状態として動く。取り込み・ダウンロード・サムネイル表示など実際にDriveへアクセスする機能は「Google Driveが未接続です」というエラーになる)。

## Google OAuth設定

`.env` にはOAuthクライアント情報を書かない。すべて `/setup`(初回)・`/settings`(以降の変更)からアプリ内で設定する。

1. [Google Cloud Console](https://console.cloud.google.com/) でプロジェクトを作成し、「Google Drive API」を有効化する。
2. 「APIとサービス」→「認証情報」で OAuth クライアントID(種類: ウェブアプリケーション)を作成する。このとき「承認済みのリダイレクトURI」は一旦空でよい(次のステップで確定する)。
3. アプリにアクセスすると `/setup` が表示されるので、Client ID・Client Secret・リダイレクトURI(既定でこの画面のURLから自動入力される)を入力する。表示されたリダイレクトURIをGoogle Cloud Console側の「承認済みのリダイレクトURI」に追加してから保存する。
4. 保存すると自動的にGoogleの認可画面に進む。初回はDriveに新規データベースが作成される。ボリューム消失後の復旧など、既存のDriveデータベースを使う場合は同じ画面の「既存のDriveデータベースファイルID」にIDを入力してから認可する。
5. Client ID/Secretやログインパスワードは後から `/settings` でいつでも変更できる。

OAuthクライアント情報・ログインパスワード・トークン暗号化キー・セッション署名キーは `${DATA_DIR}/instance_config.json` に保存され、Driveには同期されない(Driveのバックアップが漏れてもこれらは含まれない)。

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

- Egg設定で `DATA_DIR`(既定 `/data`)を永続ボリュームにマッピングすること。`instance_config.json` もこの中に保存されるので、ボリュームが消えない限りOAuthクライアント情報やログインパスワードは再設定不要。
- 環境変数は `.env.example` の3つ(`DATA_DIR` / `PORT` / `LOG_LEVEL`)のみ。Google OAuthクライアント・ログインパスワード・アップロード上限・Drive同期間隔はデプロイ後にブラウザから `/setup`・`/settings` で設定する。
- ボリュームを失って作り直した場合は、控えておいたDriveデータベースファイルID(`/settings` に表示される)を新しい `/setup` 画面で入力すれば復元できる。
- インターネットに公開する場合は `/setup` または `/settings` でログインパスワードを設定すること(下記参照)。

### 「Generic Python」系Egg(parkervcp/yolks:python_3.13など)を使う場合

Dockerfileを使わず、リポジトリをgit cloneして `pip install -r requirements.txt` → `python main.py` を実行するタイプのEggでも動くように、リポジトリ直下に `requirements.txt`(`uv export`で生成)と `main.py`(uvicornを単一ワーカーで起動するだけの薄いエントリポイント)を用意してある。Eggの変数で以下を設定する:

- `PY_FILE` = `main.py`
- `REQUIREMENTS_FILE` = `requirements.txt`
- Egg側の起動コマンドが `pip install --prefix .local` を使う場合、それが自動でパスに反映されない構成もあるので起動ログでimportエラーが出ないか確認すること。
- ポートはEggが `SERVER_PORT` として渡す想定(`main.py` はそれを最優先で読む。無ければ `PORT`、それも無ければ8000)。
- `requirements.txt` は `uv.lock` から生成しているため、依存関係を更新したら `uv export --no-hashes --no-dev --no-emit-project -o requirements.txt` で再生成すること。

このEggでは `uv` や単一ワーカーの強制がDockerfileほど厳密に保証されないため、可能であればDockerベースのEgg運用を推奨する。

## セキュリティ

- **OAuthトークン**: SQLite内に `instance_config.json` 内の鍵(Fernet、初回起動時に自動生成)で暗号化して保存する。ログには一切出力しない(`app/logging_conf.py` のredactionフィルタでも二重に保護)。最終的なセキュリティはホスト・Googleアカウント自体の保護に依存する、個人ツールとしての現実的な妥協点。
- **`instance_config.json`**: Google OAuthクライアント情報・ログインパスワード・トークン暗号化鍵・セッション署名鍵をローカルの `${DATA_DIR}/instance_config.json` に保存する。この設計はDrive同期対象のSQLiteとは意図的に分離している — Drive上のDBバックアップが漏れても、これらの値は含まれない。
- **アップロード検証**: 拡張子allowlist・サイズ上限・マジックバイト検証を行う。zipの中身は一切展開・列挙しない(zip爆弾・パストラバーサル対策)。
- **CSRF対策**: セッションに紐づくトークンを発行し、HTMXリクエストは `X-CSRF-Token` ヘッダー、通常フォーム送信は隠しフィールドで検証する(状態変更を伴う全POST/DELETEに適用)。
- **ログインゲート**: `/setup` または `/settings` でログインパスワードを設定すると、初回セットアップ(`/setup`・`/oauth/*`)以外の全ルートがパスワード保護される。個人利用でPterodactyl等インターネットに公開する場合は設定を強く推奨。パスワードを新規設定/変更した直後は、設定した本人のセッションも含めて再ログインが必要になる。
- **整合性チェック**: `/settings` 画面から、DB上のファイル参照が実際にDrive上に存在するかを確認できる(取り込み失敗時の補償削除がさらに失敗した場合などを検出する保険)。
- `.env` / `instance_config.json` は絶対にコミットしないこと(`.gitignore`済み)。

## アーキテクチャ

- `app/config.py` - `.env`(DATA_DIR/PORT/LOG_LEVEL)のみを扱う
- `app/core/instance_config.py` - ローカル専用の設定(OAuthクライアント情報・鍵・ログインパスワード)。`/setup`・`/settings` から読み書きする
- `app/services/app_config_service.py` - DB同期される運用設定(アップロード上限・Drive同期間隔)
- `app/models/` - SQLAlchemy 2.0モデル
- `app/schemas/` - Pydantic DTO
- `app/services/` - ビジネスロジック(DB・Driveへのアクセスはここに集約)
- `app/drive/` - `DriveClient` インターフェースと実装(`GoogleDriveClient` / `FakeDriveClient`)
- `app/web/pages/` - フルページ(HTML)ルート
- `app/web/fragments/` - HTMX向け部分HTMLルート
- `app/api/routers/` - JSON/バイナリ API(ダウンロード、OAuthコールバック)
- `alembic/` - DBマイグレーション

ルーターはサービス層のみを呼び出し、ORMクエリやDrive呼び出しを直接書かない方針。
