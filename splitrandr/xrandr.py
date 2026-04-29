# SplitRandR -- Split Monitor Layout Editor
# Based on ARandR by chrysn <chrysn@fsfe.org>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""Wrapper around command line xrandr with --setmonitor support.

The class body is composed via mixins from ``xrandr_invoke``,
``xrandr_load`` and ``xrandr_save``; the inner data types live in
``xrandr_types`` and are re-attached as nested attributes here so
existing ``self.State.Output(...)`` and ``cls.OutputConfiguration(...)``
lookups keep working unchanged.
"""

import os
import logging

log = logging.getLogger('splitrandr')

from .xrandr_types import Feature, State, Configuration
from .xrandr_invoke import XRandRInvokeMixin
from .xrandr_load import XRandRLoadMixin
from .xrandr_save import XRandRSaveMixin, _restart_sn_watcher


class XRandR(XRandRInvokeMixin, XRandRLoadMixin, XRandRSaveMixin):
    configuration = None
    state = None

    def __init__(self, display=None, force_version=False):
        self.environ = dict(os.environ)
        if display:
            self.environ['DISPLAY'] = display

        version_output = self._output("--version")
        supported_versions = ["1.2", "1.3", "1.4", "1.5"]
        if not any(x in version_output for x in supported_versions) and not force_version:
            raise Exception("XRandR %s required." %
                            "/".join(supported_versions))

        self.features = set()
        if " 1.2" not in version_output:
            self.features.add(Feature.PRIMARY)

    def _get_outputs(self):
        assert self.state.outputs.keys() == self.configuration.outputs.keys()
        return self.state.outputs.keys()
    outputs = property(_get_outputs)


# Re-attach the inner types so ``self.State`` / ``self.Configuration``
# / ``self.State.Output(...)`` / ``cls.OutputConfiguration(...)`` lookups
# keep resolving the same way they did when these were nested classes.
XRandR.State = State
XRandR.Configuration = Configuration
