"""
SplitRandR -- Split Monitor Layout Editor
Based on ARandR by chrysn <chrysn@fsfe.org>

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.
"""

import gi
gi.require_version('Gtk', '3.0')
gi.require_version('PangoCairo', '1.0')
from gi.repository import GObject, Gtk, Pango, PangoCairo, Gdk, GLib
import cairo

from .snap import Snap
from .xrandr import XRandR, Feature
from .auxiliary import Position, NORMAL, ROTATIONS, InadequateConfiguration
from .splits import SplitTree, SplitEditorDialog, SPLIT_COLORS
from .cinnamon_compat import query_cinnamon_monitors
from .i18n import _

# CSS for themed monitor rendering
_CSS = b"""
.splitrandr-monitor {
    background-color: @theme_bg_color;
    border: 2px solid @borders;
    border-radius: 6px;
    color: @theme_fg_color;
}
.splitrandr-monitor:hover {
    background-color: @theme_selected_bg_color;
}
"""
_css_provider = Gtk.CssProvider()
try:
    _css_provider.load_from_data(_CSS)
    Gtk.StyleContext.add_provider_for_screen(
        Gdk.Screen.get_default(),
        _css_provider,
        Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
    )
except Exception:
    pass


def _get_theme_colors():
    """Get colors from the current GTK theme."""
    ctx = Gtk.StyleContext()
    ctx.add_class('splitrandr-monitor')

    # Create a temporary widget to get a valid style context
    w = Gtk.Button()
    ctx = w.get_style_context()
    ctx.add_class('splitrandr-monitor')

    fg = ctx.get_color(Gtk.StateFlags.NORMAL)

    # Use hardcoded dark-theme-friendly defaults since
    # get_background_color/get_border_color are deprecated in GTK 3.
    return {
        'bg': (0.25, 0.25, 0.25, 0.85),
        'fg': (fg.red, fg.green, fg.blue, fg.alpha),
        'bg_hover': (0.35, 0.35, 0.35, 0.85),
        'border': (0.5, 0.5, 0.5, 1.0),
    }


def _rounded_rect(cr, x, y, w, h, r=6):
    """Draw a rounded rectangle path."""
    cr.new_sub_path()
    cr.arc(x + w - r, y + r, r, -1.5708, 0)
    cr.arc(x + w - r, y + h - r, r, 0, 1.5708)
    cr.arc(x + r, y + h - r, r, 1.5708, 3.14159)
    cr.arc(x + r, y + r, r, 3.14159, 4.71239)
    cr.close_path()


class _MonitorIdentifier(Gtk.Window):
    """Temporary overlay shown on the physical monitor to help identify it."""

    def __init__(self, name, x, y, w, h):
        super().__init__(type=Gtk.WindowType.POPUP)
        self.set_decorated(False)
        self.set_skip_taskbar_hint(True)
        self.set_skip_pager_hint(True)
        self.set_keep_above(True)
        self.set_accept_focus(False)

        screen = self.get_screen()
        visual = screen.get_rgba_visual()
        if visual:
            self.set_visual(visual)
        self.set_app_paintable(True)

        self._name = name
        self.move(x, y)
        self.resize(w, h)
        self.connect('draw', self._on_draw)
        self.show_all()

        # Make the overlay click-through by setting an empty input region
        empty_region = cairo.Region(cairo.RectangleInt(0, 0, 0, 0))
        self.get_window().input_shape_combine_region(empty_region, 0, 0)

    def _on_draw(self, widget, cr):
        cr.set_source_rgba(0, 0, 0, 0)
        cr.set_operator(cairo.OPERATOR_SOURCE)
        cr.paint()
        cr.set_operator(cairo.OPERATOR_OVER)

        alloc = self.get_allocation()
        w, h = alloc.width, alloc.height

        # Blue border frame
        cr.set_source_rgba(0.2, 0.5, 0.9, 0.7)
        cr.set_line_width(12)
        cr.rectangle(6, 6, w - 12, h - 12)
        cr.stroke()

        # Name text in center
        layout = PangoCairo.create_layout(cr)
        desc = Pango.FontDescription("sans bold 64")
        layout.set_font_description(desc)
        layout.set_text(self._name, -1)
        text_w, text_h = layout.get_pixel_size()

        # Dark backdrop behind text
        pad = 24
        cr.set_source_rgba(0, 0, 0, 0.65)
        cr.rectangle(
            (w - text_w) / 2 - pad, (h - text_h) / 2 - pad,
            text_w + 2 * pad, text_h + 2 * pad
        )
        cr.fill()

        # White text
        cr.set_source_rgba(1, 1, 1, 1)
        cr.move_to((w - text_w) / 2, (h - text_h) / 2)
        PangoCairo.show_layout(cr, layout)

        return True


class MonitorWidget(Gtk.DrawingArea):

    _draggingoutput = None
    _draggingfrom = None
    _draggingsnap = None
    _hover_output = None
    _selected_output = None
    _indicator = None
    _indicator_timer = None

    __gsignals__ = {
        'changed': (GObject.SignalFlags.RUN_LAST, GObject.TYPE_NONE, ()),
        'selection-changed': (GObject.SignalFlags.RUN_LAST, GObject.TYPE_NONE, ()),
    }

    @property
    def selected_output(self):
        return self._selected_output

    @selected_output.setter
    def selected_output(self, name):
        if name != self._selected_output:
            self._selected_output = name
            self._force_repaint()
            self.emit('selection-changed')
            if not self._readonly:
                self._show_monitor_indicator(name)

    def __init__(self, window, factor=8, display=None, force_version=False,
                 readonly=False, show_splits=True, share_xrandr_with=None):
        super(MonitorWidget, self).__init__()

        self.window = window
        self._factor = factor
        self._readonly = readonly
        # When False, render the underlying physical layout — ignore
        # cfg.splits and cfg.borders. Used by the "Real (no virtual)"
        # pane to show what xrandr would see without setmonitor/fakexrandr.
        self._show_splits = show_splits
        self._theme_colors = _get_theme_colors()
        self._screenshots = {}
        self._monitors = []
        self._is_cinnamon = False

        self.set_size_request(
            1024 // self.factor, 1024 // self.factor
        )

        self._split_drag = None  # (output_name, split_node) while resizing in main view

        if not readonly:
            self.connect('button-press-event', self.click)
            self.set_events(
                Gdk.EventMask.BUTTON_PRESS_MASK |
                Gdk.EventMask.BUTTON_RELEASE_MASK |
                Gdk.EventMask.POINTER_MOTION_MASK
            )
            self.connect('motion-notify-event', self._on_motion)
            self.connect('button-release-event', self._on_release)
            self.setup_draganddrop()

        if share_xrandr_with is not None:
            # Share the editable widget's XRandR. Avoids duplicate
            # xrandr --verbose / --listmonitors queries on every load
            # and keeps the Real pane in lockstep with the Proposed
            # pane's current_cfg state.
            self._xrandr = share_xrandr_with._xrandr
        else:
            self._xrandr = XRandR(display=display, force_version=force_version)

        self.connect('draw', self.do_expose_event)

    #################### widget features ####################

    def _set_factor(self, fac):
        self._factor = fac
        self._update_size_request()
        self._force_repaint()

    factor = property(lambda self: self._factor, _set_factor)

    def abort_if_unsafe(self):
        if not [x for x in self._xrandr.configuration.outputs.values() if x.active]:
            dialog = Gtk.MessageDialog(
                None, Gtk.DialogFlags.MODAL, Gtk.MessageType.WARNING, Gtk.ButtonsType.YES_NO,
                _(
                    "Your configuration does not include an active monitor. "
                    "Do you want to apply the configuration?"
                )
            )
            result = dialog.run()
            dialog.destroy()
            if result == Gtk.ResponseType.YES:
                return False
            return True
        return False

    def error_message(self, message):
        dialog = Gtk.MessageDialog(
            None, Gtk.DialogFlags.MODAL,
            Gtk.MessageType.ERROR, Gtk.ButtonsType.CLOSE,
            message
        )
        dialog.run()
        dialog.destroy()

    def _sync_monitors(self):
        """Build _monitors list from _xrandr.configuration."""
        self._monitors = []
        cfg = self._xrandr.configuration
        for name in sorted(cfg.outputs):
            out = cfg.outputs[name]
            if not out.active:
                continue
            pos = (out.tentative_position if hasattr(out, 'tentative_position')
                   else out.position)
            self._monitors.append({
                'name': name,
                'x': pos[0], 'y': pos[1],
                'w': out.size[0], 'h': out.size[1],
                'primary': out.primary,
                'rotation': out.rotation,
                'splits': cfg.splits.get(name) if self._show_splits else None,
                'border': cfg.borders.get(name, 0) if self._show_splits else 0,
            })

    def _update_size_request(self):
        if not self._monitors:
            self.set_size_request(128, 128)
            return
        max_x = max(m['x'] + m['w'] for m in self._monitors)
        max_y = max(m['y'] + m['h'] for m in self._monitors)
        # 10% margin
        cw = int(max_x * 1.1)
        ch = int(max_y * 1.1)
        self.set_size_request(cw // self.factor, ch // self.factor)

    #################### screenshots ####################

    def _capture_screenshots(self):
        """Capture a screenshot of each active output from the root window."""
        self._screenshots = {}
        try:
            root = Gdk.get_default_root_window()
            if root is None:
                return
        except Exception:
            return

        for m in self._monitors:
            name = m['name']
            x, y, w, h = m['x'], m['y'], m['w'], m['h']
            try:
                pb = Gdk.pixbuf_get_from_window(root, x, y, w, h)
                if pb:
                    self._screenshots[name] = pb
            except Exception:
                pass

        self._force_repaint()

    #################### monitor identifier overlay ####################

    def _show_monitor_indicator(self, output_name):
        self._hide_monitor_indicator()
        if output_name is None:
            return
        phys, leaf_idx = self.parse_virtual_name(output_name)
        cfg = self._xrandr.configuration.outputs.get(phys)
        if not cfg or not cfg.active:
            return
        x, y = cfg.position
        w, h = cfg.size
        if leaf_idx is not None:
            tree = self._xrandr.configuration.splits.get(phys)
            if tree:
                regions = list(tree.leaf_regions(w, h))
                if 0 <= leaf_idx < len(regions):
                    rx, ry, rw, rh, _, _ = regions[leaf_idx]
                    x, y, w, h = x + rx, y + ry, rw, rh
        try:
            self._indicator = _MonitorIdentifier(output_name, x, y, w, h)
            self._indicator_timer = GLib.timeout_add(2000, self._hide_monitor_indicator)
        except Exception:
            pass

    def _hide_monitor_indicator(self):
        if self._indicator:
            self._indicator.destroy()
            self._indicator = None
        if self._indicator_timer:
            GLib.source_remove(self._indicator_timer)
            self._indicator_timer = None
        return False

    #################### loading ####################

    def load_from_x(self):
        self._xrandr.load_from_x()
        self._xrandr_was_reloaded()

    def _xrandr_was_reloaded(self):
        self._is_cinnamon = False
        self._sync_monitors()

        # Validate selection: accept either a physical name or a virtual
        # name whose parent is still active and split.
        active_names = [m['name'] for m in self._monitors]
        sel = self._selected_output
        valid = False
        if sel:
            phys, leaf_idx = self.parse_virtual_name(sel)
            if leaf_idx is None and sel in active_names:
                valid = True
            elif leaf_idx is not None and phys in active_names:
                tree = self._xrandr.configuration.splits.get(phys)
                if tree and not tree.is_leaf:
                    leaf_count = sum(1 for _ in tree.iter_leaves())
                    valid = 0 <= leaf_idx < leaf_count
        if not valid:
            if len(active_names) == 1:
                self._selected_output = active_names[0]
            else:
                self._selected_output = None

        self._update_size_request()
        if self.window:
            self._force_repaint()
        self.emit('changed')
        self.emit('selection-changed')

        # Capture screenshots after a brief delay to let the display settle
        GLib.timeout_add(200, self._capture_screenshots)

    def load_from_cinnamon(self):
        """Load monitor layout from Cinnamon's compositor via DBUS.

        Falls back to load_from_x() if Cinnamon is not available.
        """
        monitors = query_cinnamon_monitors()
        if monitors is None:
            self._is_cinnamon = False
            self.load_from_x()
            return

        self._is_cinnamon = True
        self._monitors = []
        for m in monitors:
            self._monitors.append({
                'name': m['name'],
                'x': m['x'], 'y': m['y'],
                'w': m['width'], 'h': m['height'],
                'primary': m.get('primary', False),
                'rotation': None,
                'splits': None,
                'border': 0,
            })

        # Validate selection
        names = [m['name'] for m in self._monitors]
        if self._selected_output not in names:
            self._selected_output = None

        self._update_size_request()
        if self.window:
            self._force_repaint()
        self.emit('changed')
        self.emit('selection-changed')

    def save_to_x(self):
        self._xrandr.save_to_x()
        # load_from_x() now layers splits from cinnamon + layout.json
        # internally, so the trailing reload picks up the just-applied
        # state without any explicit re-merge here. Cinnamon's
        # MetaMonitor list — refreshed by _xrandr.save_to_x's restart —
        # is the authoritative source; layout.json (still holding the
        # OLD splits at this point because gui_app_apply writes it
        # AFTER this method returns) only fills outputs Cinnamon
        # doesn't currently surface.
        self.load_from_x()

    #################### doing changes ####################

    def _set_something(self, which, output_name, data):
        old = getattr(self._xrandr.configuration.outputs[output_name], which)
        setattr(self._xrandr.configuration.outputs[output_name], which, data)
        try:
            self._xrandr.check_configuration()
        except InadequateConfiguration:
            setattr(self._xrandr.configuration.outputs[output_name], which, old)
            raise

        self._sync_monitors()
        self._force_repaint()
        self.emit('changed')

    def set_position(self, output_name, pos):
        self._set_something('position', output_name, pos)

    def set_rotation(self, output_name, rot):
        self._set_something('rotation', output_name, rot)

    def set_resolution(self, output_name, res):
        self._set_something('mode', output_name, res)

    @staticmethod
    def parse_virtual_name(name):
        """Split a name like 'DP-5~2' into ('DP-5', 1) (zero-indexed leaf).
        Returns (name, None) if not a virtual."""
        if name and '~' in name:
            base, idx = name.rsplit('~', 1)
            try:
                return (base, int(idx) - 1)
            except ValueError:
                pass
        return (name, None)

    def set_primary(self, output_name, primary, leaf_idx=None):
        """Toggle primary on a physical output, optionally targeting a leaf.
        Setting any primary clears primary from every other output and leaf."""
        output = self._xrandr.configuration.outputs[output_name]

        if primary:
            for other in self._xrandr.outputs:
                self._xrandr.configuration.outputs[other].primary = False
                other_tree = self._xrandr.configuration.splits.get(other)
                if other_tree is not None:
                    other_tree.clear_primary()
            output.primary = True
            if leaf_idx is not None:
                tree = self._xrandr.configuration.splits.get(output_name)
                if tree is not None:
                    tree.set_primary_at(leaf_idx)
        else:
            output.primary = False
            tree = self._xrandr.configuration.splits.get(output_name)
            if tree is not None:
                tree.clear_primary()

        self._sync_monitors()
        self._force_repaint()
        self.emit('changed')

    def set_active(self, output_name, active):
        virtual_state = self._xrandr.state.virtual
        output = self._xrandr.configuration.outputs[output_name]

        if not active and output.active:
            output.active = False
        if active and not output.active:
            if hasattr(output, 'position'):
                output.active = True
            else:
                pos = Position((0, 0))
                for mode in self._xrandr.state.outputs[output_name].modes:
                    if mode[0] <= virtual_state.max[0] and mode[1] <= virtual_state.max[1]:
                        first_mode = mode
                        break
                else:
                    raise InadequateConfiguration(
                        "Smallest mode too large for virtual.")

                output.active = True
                output.position = pos
                output.mode = first_mode
                output.rotation = NORMAL

        self._sync_monitors()
        self._force_repaint()
        self.emit('changed')

    #################### hover tracking ####################

    SPLIT_GRAB_PX = 8  # widget-pixel grab radius for split lines in the main view

    def _virtual_at(self, x, y):
        """Return the virtual display name (e.g. 'DP-5~2') at widget coords,
        or None if the click isn't inside a split sub-region."""
        real_x = x * self.factor
        real_y = y * self.factor
        for mon in reversed(self._monitors):
            tree = mon.get('splits')
            if not tree or tree.is_leaf:
                continue
            cx = real_x - mon['x']
            cy = real_y - mon['y']
            if not (0 <= cx <= mon['w'] and 0 <= cy <= mon['h']):
                continue
            for i, (rx, ry, rw, rh, _, _) in enumerate(
                    tree.leaf_regions(mon['w'], mon['h'])):
                if rx <= cx <= rx + rw and ry <= cy <= ry + rh:
                    return "%s~%d" % (mon['name'], i + 1)
        return None

    def _find_split_line(self, x, y):
        """Find a split line at widget coords (x, y).
        Returns (output_name, split_node) or None.
        Iterates topmost monitor first (last in _monitors list)."""
        real_x = x * self.factor
        real_y = y * self.factor
        threshold_real_px = self.SPLIT_GRAB_PX * self.factor

        for mon in reversed(self._monitors):
            tree = mon.get('splits')
            if not tree:
                continue
            mw, mh = mon['w'], mon['h']
            cx = real_x - mon['x']
            cy = real_y - mon['y']
            if not (0 <= cx <= mw and 0 <= cy <= mh):
                continue
            pcx = cx / mw if mw else 0
            pcy = cy / mh if mh else 0
            edge = tree.find_nearest_edge(
                pcx, pcy,
                threshold_px=threshold_real_px,
                canvas_w=mw, canvas_h=mh,
            )
            if edge:
                return (mon['name'], edge[0])
        return None

    def _on_motion(self, widget, event):
        if self._split_drag:
            self._update_split_drag(event.x, event.y)
            return

        old_hover = self._hover_output
        undermouse = self._get_point_outputs(event.x, event.y)
        if undermouse:
            # Topmost monitor in _monitors list order (last in list = on top)
            self._hover_output = [m['name'] for m in self._monitors
                                  if m['name'] in undermouse][-1]
        else:
            self._hover_output = None
        if old_hover != self._hover_output:
            self._force_repaint()

        # Cursor feedback when hovering over a draggable split line.
        win = self.get_window()
        if win:
            line = self._find_split_line(event.x, event.y)
            if line:
                cursor_name = ('col-resize' if line[1].direction == 'V'
                               else 'row-resize')
                win.set_cursor(Gdk.Cursor.new_from_name(
                    win.get_display(), cursor_name))
            else:
                win.set_cursor(None)

    SPLIT_SNAP_PERCENT = 5  # snap proportion to nearest N% during main-window drag

    @classmethod
    def _snap_proportion(cls, prop):
        step = cls.SPLIT_SNAP_PERCENT / 100.0
        snapped = round(prop / step) * step
        return max(0.05, min(0.95, snapped))

    def _update_split_drag(self, x, y):
        output_name, node = self._split_drag
        mon = next((m for m in self._monitors if m['name'] == output_name), None)
        if not mon:
            return
        tree = self._xrandr.configuration.splits.get(output_name)
        if not tree:
            return
        region = tree.find_node_region(node)
        if not region:
            return
        rx, ry, rw, rh = region
        real_x = x * self.factor - mon['x']
        real_y = y * self.factor - mon['y']
        if mon['w'] <= 0 or mon['h'] <= 0:
            return
        if node.direction == 'V':
            new_prop = (real_x / mon['w'] - rx) / rw if rw > 0 else 0.5
        else:
            new_prop = (real_y / mon['h'] - ry) / rh if rh > 0 else 0.5
        node.proportion = self._snap_proportion(new_prop)
        self._force_repaint()

    def _on_release(self, _widget, event):
        if event.button == 1 and self._split_drag is not None:
            self._split_drag = None
            # Re-enable monitor drag-and-drop after our split-resize gesture.
            self._enable_monitor_drag_source()
            self._sync_monitors()
            self._force_repaint()
            self.emit('changed')

    #################### painting ####################

    def do_expose_event(self, _event, context):
        # Black background fills entire allocation
        alloc = self.get_allocation()
        context.set_source_rgb(0, 0, 0)
        context.rectangle(0, 0, alloc.width, alloc.height)
        context.fill()

        if not self._monitors:
            return

        context.save()
        context.scale(1 / self.factor, 1 / self.factor)
        context.set_line_width(self.factor * 1.5)
        self._draw_monitors(context)
        context.restore()

    def _draw_monitors(self, context):
        """Unified draw method for all monitors in _monitors list."""
        # Gray bounding box background
        max_x = max(m['x'] + m['w'] for m in self._monitors)
        max_y = max(m['y'] + m['h'] for m in self._monitors)
        context.set_source_rgb(0.5, 0.5, 0.5)
        context.rectangle(0, 0, max_x, max_y)
        context.fill()

        colors = self._theme_colors

        for mon in self._monitors:
            name = mon['name']
            rect = (mon['x'], mon['y'], mon['w'], mon['h'])
            center = rect[0] + rect[2] / 2, rect[1] + rect[3] / 2

            is_hover = (not self._readonly and name == self._hover_output)
            is_selected = (name == self._selected_output)
            radius = min(rect[2], rect[3]) * 0.02
            radius = max(4, min(radius, 12))

            # Fill
            bg = colors['bg_hover'] if is_hover else colors['bg']
            _rounded_rect(context, rect[0], rect[1], rect[2], rect[3], radius)
            context.set_source_rgba(*bg)
            context.fill()

            # Screenshot thumbnail
            if name in self._screenshots:
                pb = self._screenshots[name]
                context.save()
                _rounded_rect(context, rect[0], rect[1], rect[2], rect[3], radius)
                context.clip()
                sx = rect[2] / pb.get_width()
                sy = rect[3] / pb.get_height()
                context.translate(rect[0], rect[1])
                context.scale(sx, sy)
                Gdk.cairo_set_source_pixbuf(context, pb, 0, 0)
                context.paint_with_alpha(0.45)
                context.restore()

            # Border stroke
            _rounded_rect(context, rect[0], rect[1], rect[2], rect[3], radius)
            if is_selected:
                context.set_source_rgba(0.2, 0.5, 0.9, 0.9)
                context.set_line_width(4)
            else:
                context.set_source_rgba(*colors['border'])
                context.set_line_width(2)
            context.stroke()

            # Split overlay
            splits = mon['splits']
            border = mon['border']
            if splits:
                # Determine selected leaf for this monitor (if any)
                selected_leaf_idx = None
                sel = self._selected_output
                if sel:
                    sel_phys, sel_leaf = self.parse_virtual_name(sel)
                    if sel_phys == name and sel_leaf is not None:
                        selected_leaf_idx = sel_leaf
                self._draw_split_overlay(
                    context, splits,
                    rect[0], rect[1], rect[2], rect[3],
                    border,
                    selected_leaf_idx=selected_leaf_idx,
                )
            elif border > 0:
                # Unsplit output with border — show inset region
                bx_frac = border / mon['w'] if mon['w'] > 0 else 0
                by_frac = border / mon['h'] if mon['h'] > 0 else 0
                px = rect[0] + bx_frac * rect[2]
                py = rect[1] + by_frac * rect[3]
                pw = max(rect[2] * (1 - 2 * bx_frac), 0)
                ph = max(rect[3] * (1 - 2 * by_frac), 0)
                context.set_source_rgba(0.4, 0.7, 0.4, 0.2)
                context.rectangle(px, py, pw, ph)
                context.fill()
                context.set_source_rgba(0.4, 0.7, 0.4, 0.5)
                context.set_line_width(1)
                context.set_dash([4, 3])
                context.rectangle(px, py, pw, ph)
                context.stroke()
                context.set_dash([])

            # Name label
            context.save()

            rotation = mon['rotation']
            is_odd_rotation = rotation and rotation.is_odd
            textwidth = rect[3] if is_odd_rotation else rect[2]
            widthperchar = textwidth / max(len(name), 1)
            textheight = int(widthperchar * 0.8)
            textheight = max(40, min(textheight, 200))

            newdescr = Pango.FontDescription("sans bold")
            newdescr.set_size(textheight * Pango.SCALE)

            name_markup = GLib.markup_escape_text(name)
            layout = PangoCairo.create_layout(context)
            layout.set_font_description(newdescr)
            if mon['primary']:
                name_markup = "<u>%s</u>" % name_markup
            layout.set_markup(name_markup, -1)

            layoutsize = layout.get_pixel_size()

            # Compute text position at center, handling rotation
            context.move_to(*center)
            if rotation:
                context.rotate(rotation.angle)
            text_x = -layoutsize[0] / 2
            text_y = -layoutsize[1] / 2

            # Dark backdrop behind text
            pad = textheight * 0.3
            context.rel_move_to(text_x - pad, text_y - pad)
            context.rel_line_to(layoutsize[0] + 2 * pad, 0)
            context.rel_line_to(0, layoutsize[1] + 2 * pad)
            context.rel_line_to(-(layoutsize[0] + 2 * pad), 0)
            context.close_path()
            context.set_source_rgba(0, 0, 0, 0.55)
            context.fill()

            # White text
            context.move_to(*center)
            if rotation:
                context.rotate(rotation.angle)
            context.rel_move_to(text_x, text_y)
            context.set_source_rgba(1, 1, 1, 0.95)
            PangoCairo.show_layout(context, layout)

            context.restore()

    def _draw_split_overlay(self, context, tree, x, y, w, h, border=0,
                            selected_leaf_idx=None):
        """Draw semi-transparent colored regions for each split sub-monitor."""
        regions = list(tree.leaf_regions_proportional())
        leaves = [leaf for _, leaf in tree.iter_leaves()]
        # Compute border as proportion of monitor dimensions
        bx_frac = border / w if w > 0 else 0
        by_frac = border / h if h > 0 else 0
        for i, (rx, ry, rw, rh) in enumerate(regions):
            ci = i % len(SPLIT_COLORS)
            color = SPLIT_COLORS[ci]

            # Apply border inset (proportional)
            if border > 0:
                rx += bx_frac
                ry += by_frac
                rw = max(rw - 2 * bx_frac, 0)
                rh = max(rh - 2 * by_frac, 0)

            px = x + rx * w
            py = y + ry * h
            pw = rw * w
            ph = rh * h

            context.set_source_rgba(*color, 0.3)
            context.rectangle(px, py, pw, ph)
            context.fill()

            is_selected = (i == selected_leaf_idx)
            is_primary = (i < len(leaves) and leaves[i].primary)

            # Border: thick blue if selected, normal dashed colored otherwise
            if is_selected:
                context.set_source_rgba(0.2, 0.5, 0.9, 0.95)
                context.set_line_width(4)
                context.rectangle(px, py, pw, ph)
                context.stroke()
            else:
                context.set_source_rgba(*color, 0.7)
                context.set_line_width(2)
                context.set_dash([6, 4])
                context.rectangle(px, py, pw, ph)
                context.stroke()
                context.set_dash([])

            # Primary marker: solid yellow corner badge
            if is_primary and pw > 12 and ph > 12:
                context.set_source_rgba(1.0, 0.85, 0.0, 0.9)
                context.rectangle(px + 4, py + 4, 10, 10)
                context.fill()
                context.set_source_rgba(0, 0, 0, 0.7)
                context.set_line_width(1)
                context.rectangle(px + 4, py + 4, 10, 10)
                context.stroke()

    def _force_repaint(self):
        self.queue_draw()

    #################### click handling ####################

    def click(self, _widget, event):
        undermouse = self._get_point_outputs(event.x, event.y)
        if event.button == 1:
            # If the click landed on a split line, start a resize drag.
            # Returning True consumes the press so GTK's default handler
            # (which kicks off monitor D&D) never runs for this gesture.
            line = self._find_split_line(event.x, event.y)
            if line:
                self._split_drag = line
                self._lastclick = (event.x, event.y)
                return True
        if event.button == 1 and undermouse:
            which = self._get_point_active_output(event.x, event.y)
            # Bring clicked monitor to top of draw order
            for i, m in enumerate(self._monitors):
                if m['name'] == which:
                    self._monitors.append(self._monitors.pop(i))
                    break

            # If the click is inside a split sub-region, select that virtual
            # display so the user can mark it primary individually.
            virtual = self._virtual_at(event.x, event.y)
            self.selected_output = virtual if virtual else which
            self._force_repaint()
        elif event.button == 1 and not undermouse:
            self.selected_output = None
        if event.button == 3:
            if undermouse:
                target = [m['name'] for m in self._monitors
                          if m['name'] in undermouse][-1]
                # If the right-click landed inside a virtual sub-region,
                # target that specific leaf so the menu's Primary toggle
                # operates per-leaf instead of on the parent monitor.
                virtual = self._virtual_at(event.x, event.y)
                if virtual and self.parse_virtual_name(virtual)[0] == target:
                    target = virtual
                menu = self._contextmenu(target)
                menu.popup(None, None, None, None, event.button, event.time)
            else:
                menu = self.contextmenu()
                menu.popup(None, None, None, None, event.button, event.time)

        self._lastclick = (event.x, event.y)

    def _get_point_outputs(self, x, y):
        x, y = x * self.factor, y * self.factor
        return {m['name'] for m in self._monitors
                if m['x'] - self.factor <= x <= m['x'] + m['w'] + self.factor
                and m['y'] - self.factor <= y <= m['y'] + m['h'] + self.factor}

    def _get_point_active_output(self, x, y):
        undermouse = self._get_point_outputs(x, y)
        if not undermouse:
            raise IndexError("No output here.")
        # Topmost in _monitors list order
        active = [m['name'] for m in self._monitors if m['name'] in undermouse][-1]
        return active

    #################### context menu ####################

    def contextmenu(self):
        menu = Gtk.Menu()
        for output_name in self._xrandr.outputs:
            output_config = self._xrandr.configuration.outputs[output_name]
            output_state = self._xrandr.state.outputs[output_name]

            i = Gtk.MenuItem(output_name)
            i.props.submenu = self._contextmenu(output_name)
            menu.add(i)

            if not output_config.active and not output_state.connected:
                i.props.sensitive = False
        menu.show_all()
        return menu

    def _contextmenu(self, output_name):
        # output_name may be a virtual like "DP-5~2"; resolve to the
        # physical parent for most operations but track leaf_idx so the
        # Primary checkbox can target the specific leaf.
        phys_name, leaf_idx = self.parse_virtual_name(output_name)
        menu = Gtk.Menu()
        output_config = self._xrandr.configuration.outputs[phys_name]
        output_state = self._xrandr.state.outputs[phys_name]

        if leaf_idx is not None:
            header = Gtk.MenuItem(label=output_name)
            header.set_sensitive(False)
            menu.add(header)
            menu.add(Gtk.SeparatorMenuItem())

        enabled = Gtk.CheckMenuItem(_("Active"))
        enabled.props.active = output_config.active
        enabled.connect('activate', lambda menuitem: self.set_active(
            phys_name, menuitem.props.active))

        menu.add(enabled)

        if output_config.active:
            if Feature.PRIMARY in self._xrandr.features:
                primary = Gtk.CheckMenuItem(_("Primary"))
                if leaf_idx is None:
                    primary.props.active = output_config.primary
                    primary.connect('activate', lambda menuitem: self.set_primary(
                        phys_name, menuitem.props.active))
                else:
                    tree = self._xrandr.configuration.splits.get(phys_name)
                    leaf_primary = (output_config.primary and tree is not None
                                    and tree.primary_leaf_index() == leaf_idx)
                    primary.props.active = leaf_primary
                    def _toggle_leaf_primary(menuitem, _phys=phys_name, _leaf=leaf_idx):
                        active = menuitem.props.active
                        self.set_primary(_phys, active,
                                         leaf_idx=_leaf if active else None)
                    primary.connect('activate', _toggle_leaf_primary)
                menu.add(primary)

            res_m = Gtk.Menu()
            for mode in output_state.modes:
                i = Gtk.CheckMenuItem(str(mode))
                i.props.draw_as_radio = True
                i.props.active = (output_config.mode.name == mode.name)

                def _res_set(_menuitem, output_name, mode):
                    try:
                        self.set_resolution(output_name, mode)
                    except InadequateConfiguration as exc:
                        self.error_message(
                            _("Setting this resolution is not possible here: %s") % exc
                        )
                i.connect('activate', _res_set, phys_name, mode)
                res_m.add(i)

            or_m = Gtk.Menu()
            for rotation in ROTATIONS:
                i = Gtk.CheckMenuItem("%s" % rotation)
                i.props.draw_as_radio = True
                i.props.active = (output_config.rotation == rotation)

                def _rot_set(_menuitem, output_name, rotation):
                    try:
                        self.set_rotation(output_name, rotation)
                    except InadequateConfiguration as exc:
                        self.error_message(
                            _("This orientation is not possible here: %s") % exc
                        )
                i.connect('activate', _rot_set, phys_name, rotation)
                if rotation not in output_state.rotations:
                    i.props.sensitive = False
                or_m.add(i)

            res_i = Gtk.MenuItem(_("Resolution"))
            res_i.props.submenu = res_m
            or_i = Gtk.MenuItem(_("Orientation"))
            or_i.props.submenu = or_m

            menu.add(res_i)
            menu.add(or_i)

            # Split monitor menu items
            menu.add(Gtk.SeparatorMenuItem())

            split_item = Gtk.MenuItem(_("Split Monitor..."))
            split_item.connect('activate', self._on_split_monitor, phys_name)
            menu.add(split_item)

            if phys_name in self._xrandr.configuration.splits:
                remove_split = Gtk.MenuItem(_("Remove Splits"))
                remove_split.connect('activate', self._on_remove_splits, phys_name)
                menu.add(remove_split)

        menu.show_all()
        return menu

    def _on_split_monitor(self, menuitem, output_name):
        output_config = self._xrandr.configuration.outputs[output_name]
        existing_tree = self._xrandr.configuration.splits.get(output_name)

        dialog = SplitEditorDialog(
            self.window,
            output_name,
            output_config.size[0],
            output_config.size[1],
            existing_tree,
        )
        response = dialog.run()

        if response == Gtk.ResponseType.OK:
            tree = dialog.split_tree
            if tree.is_leaf:
                # No splits - remove if existed
                self._xrandr.configuration.splits.pop(output_name, None)
            else:
                self._xrandr.configuration.splits[output_name] = tree
            self._sync_monitors()
            self._force_repaint()
            self.emit('changed')

        dialog.destroy()

    def _on_remove_splits(self, menuitem, output_name):
        self._xrandr.configuration.splits.pop(output_name, None)
        self._sync_monitors()
        self._force_repaint()
        self.emit('changed')

    #################### drag&drop ####################

    def _enable_monitor_drag_source(self):
        self.drag_source_set(
            Gdk.ModifierType.BUTTON1_MASK,
            [Gtk.TargetEntry.new('splitrandr-output',
                                 Gtk.TargetFlags.SAME_WIDGET, 0)],
            0
        )

    def setup_draganddrop(self):
        self._enable_monitor_drag_source()
        self.drag_dest_set(
            0,
            [Gtk.TargetEntry.new('splitrandr-output',
                                 Gtk.TargetFlags.SAME_WIDGET, 0)],
            0
        )

        self._draggingfrom = None
        self._draggingoutput = None
        self.connect('drag-begin', self._dragbegin_cb)
        self.connect('drag-motion', self._dragmotion_cb)
        self.connect('drag-drop', self._dragdrop_cb)
        self.connect('drag-end', self._dragend_cb)

        self._lastclick = (0, 0)

    def _dragbegin_cb(self, widget, context):
        try:
            output = self._get_point_active_output(*self._lastclick)
        except IndexError:
            Gtk.drag_set_icon_name(context, 'process-stop', 10, 10)
            return

        self._draggingoutput = output
        self._draggingfrom = self._lastclick
        Gtk.drag_set_icon_name(context, 'view-fullscreen', 10, 10)

        self._draggingsnap = Snap(
            self._xrandr.configuration.outputs[self._draggingoutput].size,
            self.factor * 5,
            [(Position((0, 0)), self._xrandr.state.virtual.max)] + [
                (virtual_state.position, virtual_state.size)
                for (k, virtual_state) in self._xrandr.configuration.outputs.items()
                if k != self._draggingoutput and virtual_state.active
            ]
        )

    def _dragmotion_cb(self, widget, context, x, y, time):
        if not self._draggingoutput:
            return False

        Gdk.drag_status(context, Gdk.DragAction.MOVE, time)

        rel = x - self._draggingfrom[0], y - self._draggingfrom[1]

        oldpos = self._xrandr.configuration.outputs[self._draggingoutput].position
        newpos = Position(
            (oldpos[0] + self.factor * rel[0], oldpos[1] + self.factor * rel[1]))
        self._xrandr.configuration.outputs[
            self._draggingoutput
        ].tentative_position = self._draggingsnap.suggest(newpos)
        self._sync_monitors()
        self._force_repaint()

        return True

    def _dragdrop_cb(self, widget, context, x, y, time):
        if not self._draggingoutput:
            return

        try:
            self.set_position(
                self._draggingoutput,
                self._xrandr.configuration.outputs[self._draggingoutput].tentative_position
            )
        except InadequateConfiguration:
            context.finish(False, False, time)

        context.finish(True, False, time)

    def _dragend_cb(self, widget, context):
        try:
            del self._xrandr.configuration.outputs[self._draggingoutput].tentative_position
        except (KeyError, AttributeError):
            pass
        self._draggingoutput = None
        self._draggingfrom = None
        self._sync_monitors()
        self._force_repaint()
