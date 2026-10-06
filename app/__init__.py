import os

from flask import Flask


def create_app(test_config=None):
    app = Flask(__name__, instance_relative_config=True)
    app.config.from_mapping(
        SECRET_KEY=os.environ.get("SECRET_KEY", "dev-secret-change-me"),
        DATABASE=os.environ.get(
            "DATABASE", os.path.join(app.instance_path, "blind.db")
        ),
        # 系统默认随机种子（创建研究时可覆盖，同一研究的种子固定不变）
        DEFAULT_SEED=int(os.environ.get("DEFAULT_SEED", "20260101")),
        # 揭盲理由的最短长度（字符）
        MIN_REASON_LENGTH=int(os.environ.get("MIN_REASON_LENGTH", "10")),
    )
    if test_config:
        app.config.update(test_config)

    db_dir = os.path.dirname(app.config["DATABASE"])
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)

    from . import db

    db.init_app(app)

    from . import auth, routes

    app.register_blueprint(auth.bp)
    app.register_blueprint(routes.bp)

    return app
