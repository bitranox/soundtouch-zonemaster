"""Reading this program's settings: the six layers, the scope map, and the overrides.

The archive kept all of this in one module because both commands and the test helper needed it and
nothing else did. Here it is a package of five, split by what each part answers:

    :mod:`errors`       the one refusal type everything here raises
    :mod:`loader`       where the layers are, and the merged answer
    :mod:`settings_map` which config path fills which field of the record, and back again
    :mod:`overrides`    a ``--set`` read, and merged over what the files said
    :mod:`display`      the merged configuration as something a person reads
    :mod:`deploy`       writing the shipped defaults into a layer, as files to edit

The identity the layers are found by - vendor, app and slug - is NOT here. It lives in
``__init__conf__.py`` beside the rest of the package's identity, so that the hardware tests and
this package cannot end up looking in different places while both still pass.
"""

from __future__ import annotations

__all__: list[str] = []
