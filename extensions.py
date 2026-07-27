from flask import current_app
from pymongo import MongoClient


def init_db(app):
    """Create a single MongoClient for the app's lifetime and stash the
    database handle on app.extensions so blueprints can look it up lazily
    via get_db(), without needing MONGODB_URI to be valid at import time.
    """
    client = MongoClient(app.config["MONGODB_URI"]) if app.config.get("MONGODB_URI") else None
    app.extensions["mongo_client"] = client
    app.extensions["mongo_db"] = client[app.config["MONGODB_DB"]] if client is not None else None


def get_db():
    db = current_app.extensions.get("mongo_db")
    if db is None:
        raise RuntimeError(
            "MongoDB is not configured. Set MONGODB_URI in your environment / .env file."
        )
    return db
