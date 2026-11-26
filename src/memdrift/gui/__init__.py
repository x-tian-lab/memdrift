"""The local web app: ``memdrift ui``.

It is the one part of the project a browser can use to change files, so it has
its own command and its own server instead of being folded into read-only
``serve``. ``api.py`` documents the locks every write passes.
"""
