"""Local presentation-only server for browser checks; no production DB access."""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from flask import Flask, render_template
from page_rendering import metadata, render_page

app = Flask(__name__, static_folder=str(ROOT / "static"), static_url_path="",
            template_folder=str(ROOT / "templates"))


@app.route("/")
def landing():
    return render_template("login.html", is_logged_in=False,
                           **metadata("VooVr — Modern HR teams", public=True))


@app.route("/preview/<filename>")
def preview(filename):
    if filename not in {p.name for p in (ROOT / "static").glob("*.html")}:
        return "Not found", 404
    return render_page(filename)


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5099, use_reloader=False)
