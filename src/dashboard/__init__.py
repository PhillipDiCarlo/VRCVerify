"""VRCVerify web dashboard (issue #65).

Runs on the public VPS. Deliberately holds no database credential and no
Discord bot token: everything it knows about a server it asks the bot for, over
mTLS, and every answer is authorised by the bot rather than here.
"""
