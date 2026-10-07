"""Launch boundary implementations. Everything environment-specific lives here.

Nothing in `session_manager`, `store`, or `launch_boundary` imports anything
from this package. Selecting an implementation is `registry.build_launcher`
reading configuration, and is a change to no other code.
"""
