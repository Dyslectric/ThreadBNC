"""ThreadBNC: a private bouncer + archive for Lemmy/PieFed threads.

Core invariant: remote state may change; archived observations do not disappear.
The only destructive path is expiry of *auto-captured* threads from followed
communities, which never touches manually retained threads.
"""

__version__ = "0.1.0"
