"""
SplitRandR -- Split Monitor Layout Editor
Based on ARandR by chrysn <chrysn@fsfe.org>

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.
"""

from __future__ import division
import os
import stat

import gi
gi.require_version('Gtk', '3.0')
gi.require_version('PangoCairo', '1.0')
from gi.repository import GObject, Gtk, Pango, PangoCairo, Gdk, GLib
import cairo

from .snap import Snap
from .xrandr import XRandR, Feature
from .auxiliary import Position, NORMAL, ROTATIONS, InadequateConfiguration
from .splits import SplitTree, SplitEditorDialog, SPLIT_COLORS
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


class ARandRWidget(Gtk.DrawingArea):

    sequence = None
    _lastclick = None
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
            self._show_monitor_indicator(name)

    def __init__(self, window, factor=8, display=None, force_version=False):
        super(ARandRWidget, self).__init__()

        self.window = window
        self._factor = factor
        self._theme_colors = _get_theme_colors()
        self._screenshots = {}

        self.set_size_request(
            1024 // self.factor, 1024 // self.factor
        )

        self.connect('button-press-event', self.click)
        self.set_events(
            Gdk.EventMask.BUTTON_PRESS_MASK |
            Gdk.EventMask.POINTER_MOTION_MASK
        )
        self.connect('motion-notify-event', self._on_motion)

        self.setup_draganddrop()

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

    def _update_size_request(self):
        max_gapless = sum(
            max(output.size) if output.active else 0
            for output in self._xrandr.configuration.outputs.values()
        )
        usable_size = int(max_gapless * 1.1)
        xdim = min(self._xrandr.state.virtual.max[0], usable_size)
        ydim = min(self._xrandr.state.virtual.max[1], usable_size)
        self.set_size_request(xdim // self.factor, ydim // self.factor)

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

        for output_name in self.sequence:
            cfg = self._xrandr.configuration.outputs[output_name]
            if not cfg.active:
                continue
            x, y = cfg.position
            w, h = cfg.size
            try:
                pb = Gdk.pixbuf_get_from_window(root, x, y, w, h)
                if pb:
                    self._screenshots[output_name] = pb
            except Exception:
                pass

        self._force_repaint()

    #################### monitor identifier overlay ####################

    def _show_monitor_indicator(self, output_name):
        self._hide_monitor_indicator()
        if output_name is None:
            return
        cfg = self._xrandr.configuration.outputs.get(output_name)
        if not cfg or not cfg.active:
            return
        x, y = cfg.position
        w, h = cfg.size
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

    def load_from_file(self, file):
        data = open(file).read()
        template = self._xrandr.load_from_string(data)
        self._xrandr_was_reloaded()
        return template

    def load_from_x(self):
        self._xrandr.load_from_x()
        self._xrandr_was_reloaded()
        return self._xrandr.DEFAULTTEMPLATE

    def _xrandr_was_reloaded(self):
        self.sequence = sorted(self._xrandr.outputs)
        self._lastclick = (-1, -1)

        # Validate selection
        active_outputs = [
            name for name, cfg in self._xrandr.configuration.outputs.items()
            if cfg.active
        ]
        if self._selected_output not in active_outputs:
            if len(active_outputs) == 1:
                self._selected_output = active_outputs[0]
            else:
                self._selected_output = None

        self._update_size_request()
        if self.window:
            self._force_repaint()
        self.emit('changed')
        self.emit('selection-changed')

        # Capture screenshots after a brief delay to let the display settle
        GLib.timeout_add(200, self._capture_screenshots)

    def save_to_x(self):
        self._xrandr.save_to_x()
        self.load_from_x()

    def save_to_file(self, file, template=None, additional=None):
        data = self._xrandr.save_to_shellscript_string(template, additional)
        open(file, 'w').write(data)
        os.chmod(file, stat.S_IRWXU)
        self.load_from_file(file)

    #################### doing changes ####################

    def _set_something(self, which, output_name, data):
        old = getattr(self._xrandr.configuration.outputs[output_name], which)
        setattr(self._xrandr.configuration.outputs[output_name], which, data)
        try:
            self._xrandr.check_configuration()
        except InadequateConfiguration:
            setattr(self._xrandr.configuration.outputs[output_name], which, old)
            raise

        self._force_repaint()
        self.emit('changed')

    def set_position(self, output_name, pos):
        self._set_something('position', output_name, pos)

    def set_rotation(self, output_name, rot):
        self._set_something('rotation', output_name, rot)

    def set_resolution(self, output_name, res):
        self._set_something('mode', output_name, res)

    def set_primary(self, output_name, primary):
        output = self._xrandr.configuration.outputs[output_name]

        if primary and not output.primary:
            for output_2 in self._xrandr.outputs:
                self._xrandr.configuration.outputs[output_2].primary = False
            output.primary = True
        elif not primary and output.primary:
            output.primary = False
        else:
            return

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

        self._force_repaint()
        self.emit('changed')

    #################### hover tracking ####################

    def _on_motion(self, widget, event):
        old_hover = self._hover_output
        undermouse = self._get_point_outputs(event.x, event.y)
        if undermouse:
            self._hover_output = [a for a in self.sequence if a in undermouse][-1]
        else:
            self._hover_output = None
        if old_hover != self._hover_output:
            self._force_repaint()

    #################### painting ####################

    def do_expose_event(self, _event, context):
        context.rectangle(
            0, 0,
            self._xrandr.state.virtual.max[0] // self.factor,
            self._xrandr.state.virtual.max[1] // self.factor
        )
        context.clip()

        # clear
        context.set_source_rgb(0, 0, 0)
        context.rectangle(0, 0, *self.window.get_size())
        context.fill()
        context.save()

        context.scale(1 / self.factor, 1 / self.factor)
        context.set_line_width(self.factor * 1.5)

        self._draw(self._xrandr, context)

    def _draw(self, xrandr, context):
        cfg = xrandr.configuration
        state = xrandr.state

        context.set_source_rgb(0.25, 0.25, 0.25)
        context.rectangle(0, 0, *state.virtual.max)
        context.fill()

        context.set_source_rgb(0.5, 0.5, 0.5)
        context.rectangle(0, 0, *cfg.virtual)
        context.fill()

        colors = self._theme_colors

        for output_name in self.sequence:
            output = cfg.outputs[output_name]
            if not output.active:
                continue

            rect = (output.tentative_position if hasattr(
                output, 'tentative_position') else output.position) + tuple(output.size)
            center = rect[0] + rect[2] / 2, rect[1] + rect[3] / 2

            is_hover = (output_name == self._hover_output)
            is_selected = (output_name == self._selected_output)
            radius = min(rect[2], rect[3]) * 0.02
            radius = max(4, min(radius, 12))

            # Paint themed rounded rectangle
            if is_hover:
                bg = colors['bg_hover']
            else:
                bg = colors['bg']
            _rounded_rect(context, rect[0], rect[1], rect[2], rect[3], radius)
            context.set_source_rgba(*bg)
            context.fill()

            # Draw screenshot thumbnail if available
            if output_name in self._screenshots:
                pb = self._screenshots[output_name]
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

            # Border
            _rounded_rect(context, rect[0], rect[1], rect[2], rect[3], radius)
            if is_selected:
                context.set_source_rgba(0.2, 0.5, 0.9, 0.9)
                context.set_line_width(4)
            else:
                border = colors['border']
                context.set_source_rgba(*border)
                context.set_line_width(2)
            context.stroke()

            # Draw split overlay if this output has splits
            if output_name in cfg.splits:
                self._draw_split_overlay(
                    context, cfg.splits[output_name],
                    rect[0], rect[1], rect[2], rect[3]
                )

            # Draw output name: large, bold, white on dark backdrop
            context.save()

            textwidth = rect[3 if output.rotation.is_odd else 2]
            widthperchar = textwidth / max(len(output_name), 1)
            textheight = int(widthperchar * 0.8)
            textheight = max(textheight, 40)

            newdescr = Pango.FontDescription("sans bold")
            newdescr.set_size(textheight * Pango.SCALE)

            output_name_markup = GLib.markup_escape_text(output_name)
            layout = PangoCairo.create_layout(context)
            layout.set_font_description(newdescr)
            if output.primary:
                output_name_markup = "<u>%s</u>" % output_name_markup

            layout.set_markup(output_name_markup, -1)

            layoutsize = layout.get_pixel_size()

            # Compute text position at center, handling rotation
            context.move_to(*center)
            context.rotate(output.rotation.angle)
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
            context.rotate(output.rotation.angle)
            context.rel_move_to(text_x, text_y)
            context.set_source_rgba(1, 1, 1, 0.95)
            PangoCairo.show_layout(context, layout)

            context.restore()

    def _draw_split_overlay(self, context, tree, x, y, w, h):
        """Draw semi-transparent colored regions for each split sub-monitor."""
        regions = list(tree.leaf_regions_proportional())
        for i, (rx, ry, rw, rh) in enumerate(regions):
            ci = i % len(SPLIT_COLORS)
            color = SPLIT_COLORS[ci]

            px = x + rx * w
            py = y + ry * h
            pw = rw * w
            ph = rh * h

            context.set_source_rgba(*color, 0.3)
            context.rectangle(px, py, pw, ph)
            context.fill()

            # Dashed border
            context.set_source_rgba(*color, 0.7)
            context.set_line_width(2)
            context.set_dash([6, 4])
            context.rectangle(px, py, pw, ph)
            context.stroke()
            context.set_dash([])

    def _force_repaint(self):
        self.queue_draw_area(
            0, 0,
            self._xrandr.state.virtual.max[0] // self.factor,
            self._xrandr.state.virtual.max[1] // self.factor
        )

    #################### click handling ####################

    def click(self, _widget, event):
        undermouse = self._get_point_outputs(event.x, event.y)
        if event.button == 1 and undermouse:
            which = self._get_point_active_output(event.x, event.y)
            if self._lastclick == (event.x, event.y):
                newpos = min(self.sequence.index(a) for a in undermouse)
                self.sequence.remove(which)
                self.sequence.insert(newpos, which)
                which = self._get_point_active_output(event.x, event.y)
            self.sequence.remove(which)
            self.sequence.append(which)

            self.selected_output = which
            self._lastclick = (event.x, event.y)
            self._force_repaint()
        elif event.button == 1 and not undermouse:
            self.selected_output = None
        if event.button == 3:
            if undermouse:
                target = [a for a in self.sequence if a in undermouse][-1]
                menu = self._contextmenu(target)
                menu.popup(None, None, None, None, event.button, event.time)
            else:
                menu = self.contextmenu()
                menu.popup(None, None, None, None, event.button, event.time)

        self._lastclick = (event.x, event.y)

    def _get_point_outputs(self, x, y):
        x, y = x * self.factor, y * self.factor
        outputs = set()
        for output_name, output in self._xrandr.configuration.outputs.items():
            if not output.active:
                continue
            if (
                    output.position[0] - self.factor <= x <= output.position[0] + output.size[0] + self.factor
            ) and (
                output.position[1] - self.factor <= y <= output.position[1] + output.size[1] + self.factor
            ):
                outputs.add(output_name)
        return outputs

    def _get_point_active_output(self, x, y):
        undermouse = self._get_point_outputs(x, y)
        if not undermouse:
            raise IndexError("No output here.")
        active = [a for a in self.sequence if a in undermouse][-1]
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
        menu = Gtk.Menu()
        output_config = self._xrandr.configuration.outputs[output_name]
        output_state = self._xrandr.state.outputs[output_name]

        enabled = Gtk.CheckMenuItem(_("Active"))
        enabled.props.active = output_config.active
        enabled.connect('activate', lambda menuitem: self.set_active(
            output_name, menuitem.props.active))

        menu.add(enabled)

        if output_config.active:
            if Feature.PRIMARY in self._xrandr.features:
                primary = Gtk.CheckMenuItem(_("Primary"))
                primary.props.active = output_config.primary
                primary.connect('activate', lambda menuitem: self.set_primary(
                    output_name, menuitem.props.active))
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
                i.connect('activate', _res_set, output_name, mode)
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
                i.connect('activate', _rot_set, output_name, rotation)
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
            split_item.connect('activate', self._on_split_monitor, output_name)
            menu.add(split_item)

            if output_name in self._xrandr.configuration.splits:
                remove_split = Gtk.MenuItem(_("Remove Splits"))
                remove_split.connect('activate', self._on_remove_splits, output_name)
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
            self._force_repaint()
            self.emit('changed')

        dialog.destroy()

    def _on_remove_splits(self, menuitem, output_name):
        self._xrandr.configuration.splits.pop(output_name, None)
        self._force_repaint()
        self.emit('changed')

    #################### drag&drop ####################

    def setup_draganddrop(self):
        self.drag_source_set(
            Gdk.ModifierType.BUTTON1_MASK,
            [Gtk.TargetEntry.new('splitrandr-output',
                                 Gtk.TargetFlags.SAME_WIDGET, 0)],
            0
        )
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
        self._force_repaint()
