"""The service's command line: a group that holds the zone, with two config commands beside it.

    root.py        the group itself, its options, and the run it performs with no subcommand
    config_cmd.py  ``config``: the merged configuration, and where each value came from
    deploy_cmd.py  ``config-deploy``: the shipped defaults written into a layer, as files to edit

A group rather than a single command so that the two config commands can sit beside the run, and
``invoke_without_command`` so that an argv naming only options still holds the zone - which is what
the deployed unit sends.
"""

from __future__ import annotations

__all__: list[str] = []
