# SplitRandR -- Split Monitor Layout Editor
# Based on ARandR by chrysn <chrysn@fsfe.org>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""Inner data types for XRandR (State, Configuration). Imported and
re-exported from ``xrandr.py`` and assigned as ``XRandR.State`` /
``XRandR.Configuration`` so existing ``self.State.Output(...)`` and
``cls.OutputConfiguration(...)`` lookups keep working unchanged.
"""

from .auxiliary import Size, Geometry, NamedSize, Rotation
from .splits import SplitTree


class Feature:
    PRIMARY = 1


class State:
    """Represents everything that can not be set by xrandr."""

    virtual = None

    def __init__(self):
        self.outputs = {}

    def __repr__(self):
        return '<%s for %d Outputs, %d connected>' % (
            type(self).__name__, len(self.outputs),
            len([x for x in self.outputs.values() if x.connected])
        )

    class Virtual:
        def __init__(self, min_mode, max_mode):
            self.min = min_mode
            self.max = max_mode

    class Output:
        rotations = None
        connected = None
        physical_w_mm = 0
        physical_h_mm = 0
        edid_hex = ""

        def __init__(self, name):
            self.name = name
            self.modes = []
            self.preferred_resolution = None  # (w, h) tuple

        def __repr__(self):
            return '<%s %r (%d modes)>' % (type(self).__name__, self.name, len(self.modes))

        def modes_by_resolution(self):
            """Return {(w,h): [NamedSize, ...]} grouped by resolution, sorted by rate desc."""
            grouped = {}
            for mode in self.modes:
                key = (mode.width, mode.height)
                if key not in grouped:
                    grouped[key] = []
                grouped[key].append(mode)
            for key in grouped:
                grouped[key].sort(
                    key=lambda m: m.refresh_rate if m.refresh_rate is not None else 0,
                    reverse=True
                )
            return grouped


class Configuration:
    """Represents everything that can be set by xrandr."""

    virtual = None

    def __init__(self, xrandr):
        self.outputs = {}
        self.splits = {}
        self.borders = {}
        self._xrandr = xrandr

    def __repr__(self):
        return '<%s for %d Outputs, %d active>' % (
            type(self).__name__, len(self.outputs),
            len([x for x in self.outputs.values() if x.active])
        )

    def commandlineargs(self):
        args = []
        # Pre-set the framebuffer size so the nvidia driver doesn't
        # misplace outputs when resizing the screen.  Without this,
        # nvidia processes outputs sequentially and may temporarily
        # shrink the screen, making later output positions invalid.
        fb_w = 0
        fb_h = 0
        for output in self.outputs.values():
            if output.active:
                fb_w = max(fb_w, output.position[0] + output.size[0])
                fb_h = max(fb_h, output.position[1] + output.size[1])
        if fb_w > 0 and fb_h > 0:
            args.extend(["--fb", "%dx%d" % (fb_w, fb_h)])
        for output_name, output in self.outputs.items():
            args.append("--output")
            args.append(output_name)
            if not output.active:
                args.append("--off")
            else:
                if Feature.PRIMARY in self._xrandr.features:
                    if output.primary:
                        args.append("--primary")
                args.append("--mode")
                args.append(str(output.mode.name))
                if output.mode.refresh_rate is not None:
                    args.append("--rate")
                    args.append("%.2f" % output.mode.refresh_rate)
                args.append("--pos")
                args.append(str(output.position))
                args.append("--rotate")
                args.append(output.rotation)
        return args

    def to_dict(self):
        outputs = {}
        for name, out in self.outputs.items():
            d = {'active': out.active, 'primary': out.primary}
            if out.active:
                d['mode'] = out.mode.name
                d['refresh_rate'] = out.mode.refresh_rate
                d['position'] = list(out.position)
                d['rotation'] = str(out.rotation)
            outputs[name] = d
        splits = {}
        for name, tree in self.splits.items():
            splits[name] = tree.to_dict()
        return {
            'outputs': outputs,
            'splits': splits,
            'borders': dict(self.borders),
            'pre_commands': getattr(self, '_pre_commands', []),
        }

    @classmethod
    def from_dict(cls, data, xrandr):
        cfg = cls(xrandr)
        for name, out_data in data.get('outputs', {}).items():
            active = out_data['active']
            primary = out_data.get('primary', False)
            if active:
                mode_name = out_data['mode']
                refresh_rate = out_data.get('refresh_rate')
                pos = out_data['position']
                rotation = Rotation(out_data.get('rotation', 'normal'))
                # Find mode from state if available
                mode = None
                state_out = xrandr.state.outputs.get(name) if xrandr.state else None
                if state_out:
                    for m in state_out.modes:
                        if m.name == mode_name:
                            if refresh_rate is not None and m.refresh_rate is not None:
                                if abs(m.refresh_rate - refresh_rate) < 0.1:
                                    mode = m
                                    break
                            elif mode is None:
                                mode = m
                if mode is None:
                    # Parse resolution from mode name (e.g. "3840x2160")
                    parts = mode_name.split('x')
                    if len(parts) == 2:
                        try:
                            w, h = int(parts[0]), int(parts[1])
                            mode = NamedSize(Size([w, h]), name=mode_name,
                                             refresh_rate=refresh_rate)
                        except ValueError:
                            mode = NamedSize(Size([1920, 1080]), name=mode_name,
                                             refresh_rate=refresh_rate)
                    else:
                        mode = NamedSize(Size([1920, 1080]), name=mode_name,
                                         refresh_rate=refresh_rate)
                geometry = Geometry(mode[0], mode[1], pos[0], pos[1])
                if rotation.is_odd:
                    geometry = Geometry(mode[1], mode[0], pos[0], pos[1])
                oc = cls.OutputConfiguration(
                    active=True, primary=primary,
                    geometry=geometry, rotation=rotation,
                    modename=mode_name, refresh_rate=refresh_rate,
                )
            else:
                oc = cls.OutputConfiguration(
                    active=False, primary=False,
                    geometry=None, rotation=None,
                    modename=None, refresh_rate=None,
                )
            cfg.outputs[name] = oc
        for name, tree_data in data.get('splits', {}).items():
            cfg.splits[name] = SplitTree.from_dict(tree_data)
        cfg.borders = data.get('borders', {})
        cfg._pre_commands = data.get('pre_commands', [])
        return cfg

    class OutputConfiguration:

        def __init__(self, active, primary, geometry, rotation, modename, refresh_rate=None):
            self.active = active
            self.primary = primary
            if active:
                self.position = geometry.position
                self.rotation = rotation
                if rotation.is_odd:
                    self.mode = NamedSize(
                        Size(reversed(geometry.size)), name=modename, refresh_rate=refresh_rate)
                else:
                    self.mode = NamedSize(geometry.size, name=modename, refresh_rate=refresh_rate)

        size = property(lambda self: NamedSize(
            Size(reversed(self.mode)), name=self.mode.name
        ) if self.rotation.is_odd else self.mode)
