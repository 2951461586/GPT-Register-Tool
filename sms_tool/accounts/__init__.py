"""Account lifecycle, liveness, promotion and recovery workflows.

These modules used to live flat in ``sms_tool/``.  They were grouped here
because they all operate on one stored account record.  There are
deliberately **no** ``sms_tool/account_*.py`` forwarding shells: tests
patch ``sms_tool.accounts.<module>.<name>``, and a shell would silently
soak up those patches without ever being read by production code.
"""
