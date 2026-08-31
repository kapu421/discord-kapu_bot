import os
import logging
import sys
from threading import Thread

from flask import Flask

logger = logging.getLogger(__name__)

app = Flask('')

# Flaskの標準ログを抑制（UptimeRobotのアクセスログでコンソールが埋まらないように）
logging.getLogger('werkzeug').setLevel(logging.ERROR)


@app.route('/')
def home():
    return "I'm alive!"


def run():
    """Flask サーバーを起動（エラーハンドリング付き）"""
    try:
        port = int(os.environ.get("PORT", 8080))  # Renderが割り当てるPORTを使う
        logger.info(f"Flask サーバーをポート {port} で起動します...")
        app.run(host='0.0.0.0', port=port, debug=False)
    except ValueError:
        logger.error("PORT 環境変数が整数ではありません。デフォルトの8080を使用します。")
        try:
            app.run(host='0.0.0.0', port=8080, debug=False)
        except Exception as e:
            logger.exception(f"Flask サーバーの起動に失敗しました: {e}")
            sys.exit(1)
    except OSError as e:
        logger.exception(f"ポートのバインドに失敗しました（ポート {port} が使用中の可能性があります）: {e}")
        sys.exit(1)
    except Exception as e:
        logger.exception(f"Flask サーバー実行中に予期しないエラーが発生しました: {e}")
        sys.exit(1)


def keep_alive():
    """Flask サーバーをバックグラウンドスレッドで起動（エラーハンドリング付き）"""
    try:
        t = Thread(target=run)
        t.daemon = True  # メインプロセス終了時に一緒に終了させる
        t.start()
        logger.info("keep_alive: Flask サーバーを起動しました（UptimeRobot pings 用）。")
    except Exception as e:
        logger.exception(f"Flask サーバーのスレッド起動に失敗しました: {e}")
        raise
