from flask import Blueprint, render_template

static_pages = Blueprint("static_pages", __name__)

@static_pages.route("/sector-leaders")
def sector_leaders():
    return render_template("sector_leaders.html")
