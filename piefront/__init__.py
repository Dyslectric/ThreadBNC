"""A public frontend for one PieFed server, with ThreadBNC's look.

Nothing is kept here: every page is read from the server's API as whoever is
looking (their session there is kept in their signed cookie), or as nobody for
people who haven't signed in. Styles, icons and page scripts are ThreadBNC's.
"""

__version__ = "0.1.0"
