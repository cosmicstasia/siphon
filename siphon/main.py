import typer

from siphon.scrapers import snapchat

app = typer.Typer(help="Siphon — mobile app scraper", no_args_is_help=True)
app.add_typer(snapchat.app, name="snapchat")


if __name__ == "__main__":
    app()
