# SplitRandR -- Split Monitor Layout Editor
# Based on ARandR by chrysn and fakexrandr-manage.py
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""Split tree data model and split editor dialog."""

import struct

import gi
gi.require_version('Gtk', '3.0')
from gi.repository import Gtk, Gdk

SPLIT_COLORS = [
    (int(x[:2], 16) / 255., int(x[2:4], 16) / 255., int(x[4:], 16) / 255.)
    for x in "5e412f fcebb6 78c0a8 f07818 f0a830 b1eb00 53bbf4 ff85cb ff432e ffac00".split()
]


class SplitTree:
    """Binary tree of horizontal/vertical splits stored as proportions (0.0-1.0).

    Internal representation: None for a leaf, or [direction, proportion, left, right]
    where direction is 'H' or 'V', proportion is 0.0-1.0, and left/right are SplitTree.
    """

    def __init__(self, direction=None, proportion=0.5, left=None, right=None):
        self.direction = direction  # 'H' or 'V' or None (leaf)
        self.proportion = proportion
        self.left = left or SplitTree.__new_leaf()
        self.right = right or SplitTree.__new_leaf()
        self.primary = False  # only meaningful for leaves

    @staticmethod
    def __new_leaf():
        t = SplitTree.__new__(SplitTree)
        t.direction = None
        t.proportion = 0.5
        t.left = None
        t.right = None
        t.primary = False
        return t

    @staticmethod
    def new_leaf():
        return SplitTree.__new_leaf()

    @property
    def is_leaf(self):
        return self.direction is None

    def leaf_regions(self, width, height, x=0, y=0, w_mm=0, h_mm=0):
        """Enumerate sub-monitor rectangles as (x, y, w, h, w_mm, h_mm) tuples."""
        if self.is_leaf:
            yield (x, y, width, height, w_mm, h_mm)
            return

        if self.direction == 'V':
            left_w = int(round(width * self.proportion))
            right_w = width - left_w
            left_mm = int(round(w_mm * self.proportion)) if w_mm else 0
            right_mm = w_mm - left_mm if w_mm else 0
            yield from self.left.leaf_regions(left_w, height, x, y, left_mm, h_mm)
            yield from self.right.leaf_regions(right_w, height, x + left_w, y, right_mm, h_mm)
        else:  # 'H'
            top_h = int(round(height * self.proportion))
            bottom_h = height - top_h
            top_mm = int(round(h_mm * self.proportion)) if h_mm else 0
            bottom_mm = h_mm - top_mm if h_mm else 0
            yield from self.left.leaf_regions(width, top_h, x, y, w_mm, top_mm)
            yield from self.right.leaf_regions(width, bottom_h, x, y + top_h, w_mm, bottom_mm)

    def leaf_regions_proportional(self, x=0.0, y=0.0, w=1.0, h=1.0):
        """Enumerate sub-regions as proportional (x, y, w, h) tuples in 0.0-1.0 space."""
        if self.is_leaf:
            yield (x, y, w, h)
            return

        if self.direction == 'V':
            left_w = w * self.proportion
            right_w = w - left_w
            yield from self.left.leaf_regions_proportional(x, y, left_w, h)
            yield from self.right.leaf_regions_proportional(x + left_w, y, right_w, h)
        else:
            top_h = h * self.proportion
            bottom_h = h - top_h
            yield from self.left.leaf_regions_proportional(x, y, w, top_h)
            yield from self.right.leaf_regions_proportional(x, y + top_h, w, bottom_h)

    def get_split_for_point(self, px, py, x=0.0, y=0.0, w=1.0, h=1.0):
        """Hit-test: return the (tree_node, x, y, w, h) for the leaf containing (px, py)."""
        if self.is_leaf:
            return (self, x, y, w, h)

        if self.direction == 'V':
            split_x = x + w * self.proportion
            if px < split_x:
                return self.left.get_split_for_point(px, py, x, y, w * self.proportion, h)
            else:
                return self.right.get_split_for_point(
                    px, py, split_x, y, w * (1 - self.proportion), h)
        else:
            split_y = y + h * self.proportion
            if py < split_y:
                return self.left.get_split_for_point(px, py, x, y, w, h * self.proportion)
            else:
                return self.right.get_split_for_point(
                    px, py, x, split_y, w, h * (1 - self.proportion))

    def find_nearest_edge(self, px, py, x=0.0, y=0.0, w=1.0, h=1.0,
                          threshold_px=8, canvas_w=1.0, canvas_h=1.0):
        """Find the nearest split edge to point (px, py) in proportional space.
        Distance is measured in pixels (using canvas_w/canvas_h to scale)
        so the grab radius is uniform regardless of the canvas aspect ratio.
        Returns (node, parent, is_left_child, distance_px) or None."""
        results = []
        self._collect_edges(
            px, py, x, y, w, h, canvas_w, canvas_h, None, True, results)
        if not results:
            return None
        results.sort(key=lambda r: r[3])
        if results[0][3] < threshold_px:
            return results[0]
        return None

    def _collect_edges(self, px, py, x, y, w, h, cw, ch, parent, is_left, results):
        if self.is_leaf:
            return

        if self.direction == 'V':
            edge_x = x + w * self.proportion
            if y <= py <= y + h:
                dist = abs(px - edge_x) * cw
                results.append((self, parent, is_left, dist))
            self.left._collect_edges(
                px, py, x, y, w * self.proportion, h, cw, ch, self, True, results)
            self.right._collect_edges(
                px, py, edge_x, y, w * (1 - self.proportion), h, cw, ch,
                self, False, results)
        else:
            edge_y = y + h * self.proportion
            if x <= px <= x + w:
                dist = abs(py - edge_y) * ch
                results.append((self, parent, is_left, dist))
            self.left._collect_edges(
                px, py, x, y, w, h * self.proportion, cw, ch, self, True, results)
            self.right._collect_edges(
                px, py, x, edge_y, w, h * (1 - self.proportion), cw, ch,
                self, False, results)

    def find_node_region(self, target, x=0.0, y=0.0, w=1.0, h=1.0):
        """Return the proportional (x, y, w, h) region of a target node, or None."""
        if self is target:
            return (x, y, w, h)
        if self.is_leaf:
            return None
        if self.direction == 'V':
            left_w = w * self.proportion
            return (self.left.find_node_region(target, x, y, left_w, h)
                    or self.right.find_node_region(
                        target, x + left_w, y, w - left_w, h))
        else:
            top_h = h * self.proportion
            return (self.left.find_node_region(target, x, y, w, top_h)
                    or self.right.find_node_region(
                        target, x, y + top_h, w, h - top_h))

    def to_setmonitor_commands(self, output_name, width, height, x_off, y_off, w_mm, h_mm, border=0):
        """Generate xrandr --setmonitor argument lists.
        Returns list of (monitor_name, geometry_str, output_or_none).
        First sub-monitor gets the real output name, rest get 'none'.
        If border > 0, each region is inset by that many pixels to create
        mouse dead zones between adjacent virtual monitors.
        """
        regions = list(self.leaf_regions(width, height, x_off, y_off, w_mm, h_mm))
        commands = []
        for i, (rx, ry, rw, rh, rmm_w, rmm_h) in enumerate(regions):
            if border > 0:
                rx += border
                ry += border
                rw = max(rw - 2 * border, 1)
                rh = max(rh - 2 * border, 1)
            mon_name = "%s~%d" % (output_name, i)
            geom = "%d/%dx%d/%d+%d+%d" % (rw, rmm_w, rh, rmm_h, rx, ry)
            out = output_name if i == 0 else "none"
            commands.append((mon_name, geom, out))
        return commands

    def count_leaves(self):
        if self.is_leaf:
            return 1
        return self.left.count_leaves() + self.right.count_leaves()

    @staticmethod
    def from_setmonitor_regions(regions, output_name, total_w, total_h):
        """Reconstruct a SplitTree from a list of sub-monitor rectangles.
        regions: list of (x, y, w, h) sorted by the naming convention index.
        Uses a recursive approach: finds the split that divides the regions into two groups.
        """
        if len(regions) <= 1:
            return SplitTree.new_leaf()

        # Try vertical split: find an x coordinate that divides regions
        xs = sorted(set(r[0] for r in regions) | set(r[0] + r[2] for r in regions))
        for split_x in xs:
            left_r = [r for r in regions if r[0] + r[2] <= split_x]
            right_r = [r for r in regions if r[0] >= split_x]
            if left_r and right_r and len(left_r) + len(right_r) == len(regions):
                prop = split_x / total_w if total_w else 0.5
                left_tree = SplitTree.from_setmonitor_regions(
                    left_r, output_name, split_x, total_h)
                right_w = total_w - split_x
                right_tree = SplitTree.from_setmonitor_regions(
                    [(r[0] - split_x, r[1], r[2], r[3]) for r in right_r],
                    output_name, right_w, total_h)
                tree = SplitTree('V', prop, left_tree, right_tree)
                return tree

        # Try horizontal split
        ys = sorted(set(r[1] for r in regions) | set(r[1] + r[3] for r in regions))
        for split_y in ys:
            top_r = [r for r in regions if r[1] + r[3] <= split_y]
            bottom_r = [r for r in regions if r[1] >= split_y]
            if top_r and bottom_r and len(top_r) + len(bottom_r) == len(regions):
                prop = split_y / total_h if total_h else 0.5
                top_tree = SplitTree.from_setmonitor_regions(
                    top_r, output_name, total_w, split_y)
                bottom_h = total_h - split_y
                bottom_tree = SplitTree.from_setmonitor_regions(
                    [(r[0], r[1] - split_y, r[2], r[3]) for r in bottom_r],
                    output_name, total_w, bottom_h)
                tree = SplitTree('H', prop, top_tree, bottom_tree)
                return tree

        return SplitTree.new_leaf()

    def to_fakexrandr_bytes(self, width, height):
        """Serialize to fakexrandr binary tree format.

        Format: 'N' for leaf, or ('H'|'V') + 4-byte uint split position + left + right.
        H = horizontal line (top/bottom), position = pixels from top.
        V = vertical line (left/right), position = pixels from left.
        """
        if self.is_leaf:
            return b'N'
        if self.direction == 'H':
            pos = int(round(height * self.proportion))
            return (b'H' + struct.pack('I', pos) +
                    self.left.to_fakexrandr_bytes(width, pos) +
                    self.right.to_fakexrandr_bytes(width, height - pos))
        else:  # 'V'
            pos = int(round(width * self.proportion))
            return (b'V' + struct.pack('I', pos) +
                    self.left.to_fakexrandr_bytes(pos, height) +
                    self.right.to_fakexrandr_bytes(width - pos, height))

    def to_dict(self):
        if self.is_leaf:
            # Leaves only need to round-trip if they carry state.
            return {'primary': True} if self.primary else None
        return {'d': self.direction, 'p': self.proportion,
                'l': self.left.to_dict(), 'r': self.right.to_dict()}

    @staticmethod
    def from_dict(d):
        if d is None:
            return SplitTree.new_leaf()
        if 'd' not in d:
            # Leaf payload (e.g. {'primary': True})
            leaf = SplitTree.new_leaf()
            leaf.primary = bool(d.get('primary', False))
            return leaf
        node = SplitTree(d['d'], d['p'],
                         SplitTree.from_dict(d['l']),
                         SplitTree.from_dict(d['r']))
        return node

    def copy(self):
        if self.is_leaf:
            leaf = SplitTree.new_leaf()
            leaf.primary = self.primary
            return leaf
        return SplitTree(self.direction, self.proportion,
                         self.left.copy(), self.right.copy())

    def iter_leaves(self):
        """Yield (index, leaf) in spatial enumeration order — same order as
        leaf_regions, leaf_regions_proportional, and the ~N naming convention."""
        idx = [0]

        def walk(node):
            if node.is_leaf:
                yield (idx[0], node)
                idx[0] += 1
                return
            yield from walk(node.left)
            yield from walk(node.right)

        yield from walk(self)

    def primary_leaf_index(self):
        """Return the spatial index of the leaf with primary=True, or None."""
        for i, leaf in self.iter_leaves():
            if leaf.primary:
                return i
        return None

    def clear_primary(self):
        """Unset primary on all leaves."""
        for _, leaf in self.iter_leaves():
            leaf.primary = False

    def set_primary_at(self, index):
        """Set primary on the leaf at the given spatial index, clearing others."""
        for i, leaf in self.iter_leaves():
            leaf.primary = (i == index)


class SplitEditorDialog(Gtk.Dialog):
    """Dialog for interactively editing monitor splits.
    Adapted from fakexrandr-manage.py's ConfigurationWidget.
    """

    CANVAS_WIDTH = 300

    def __init__(self, parent, output_name, width, height, split_tree=None):
        super().__init__(
            title="Split Monitor: %s" % output_name,
            transient_for=parent,
            modal=True,
        )
        self.add_buttons(
            "Cancel", Gtk.ResponseType.CANCEL,
            "OK", Gtk.ResponseType.OK,
        )

        self._output_name = output_name
        self._width = width
        self._height = height
        self._aspect_ratio = width / height if height else 16 / 9
        self._canvas_height = int(self.CANVAS_WIDTH / self._aspect_ratio)

        if split_tree:
            self._tree = split_tree.copy()
        else:
            self._tree = SplitTree.new_leaf()

        self._mouse_down_at = None
        self._drag_mode = None  # 'new_split', 'move_edge'
        self._drag_target_leaf = None
        self._drag_target_edge = None
        self._drag_decision = None  # 'H', 'V', or None

        # Undo history: stack of tree snapshots taken BEFORE each
        # mutating operation (split-creation, edge-resize, edge-removal).
        # Most-recent state is at the top of the stack.
        self._undo_stack = []

        content = self.get_content_area()

        label = Gtk.Label()
        label.set_markup(
            "<small>Drag inside a region to split it. Drag an existing line "
            "to resize. Right-click a line to remove. Ctrl+Z to undo.</small>"
        )
        label.set_margin_top(6)
        label.set_margin_bottom(6)
        content.pack_start(label, False, False, 0)

        self._drawing_area = Gtk.DrawingArea()
        self._drawing_area.set_size_request(self.CANVAS_WIDTH, self._canvas_height)
        self._drawing_area.set_events(
            Gdk.EventMask.POINTER_MOTION_MASK |
            Gdk.EventMask.BUTTON_PRESS_MASK |
            Gdk.EventMask.BUTTON_RELEASE_MASK
        )
        self._drawing_area.connect("draw", self._on_draw)
        self._drawing_area.connect("button-press-event", self._on_button_press)
        self._drawing_area.connect("button-release-event", self._on_button_release)
        self._drawing_area.connect("motion-notify-event", self._on_motion)

        frame = Gtk.Frame()
        frame.set_shadow_type(Gtk.ShadowType.IN)
        frame.add(self._drawing_area)
        frame.set_margin_start(12)
        frame.set_margin_end(12)
        frame.set_margin_bottom(12)
        content.pack_start(frame, False, False, 0)

        # Undo + Reset buttons. Reset clears all splits back to a single
        # leaf — equivalent to undoing every operation.
        button_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        button_row.set_margin_start(12)
        button_row.set_margin_end(12)
        button_row.set_margin_bottom(12)
        self._undo_button = Gtk.Button(label="Undo")
        self._undo_button.set_sensitive(False)
        self._undo_button.connect("clicked", lambda b: self._undo())
        button_row.pack_start(self._undo_button, False, False, 0)
        reset_button = Gtk.Button(label="Reset (no splits)")
        reset_button.connect("clicked", lambda b: self._reset_tree())
        button_row.pack_start(reset_button, False, False, 0)
        content.pack_start(button_row, False, False, 0)

        # Ctrl+Z keyboard accelerator
        accel = Gtk.AccelGroup()
        self.add_accel_group(accel)
        accel.connect(
            Gdk.KEY_z, Gdk.ModifierType.CONTROL_MASK, 0,
            lambda *a: (self._undo(), True)[1]
        )

        content.show_all()

    def _push_undo(self):
        """Snapshot the current tree before a mutating operation."""
        self._undo_stack.append(self._tree.copy())
        self._undo_button.set_sensitive(True)

    def _undo(self):
        if not self._undo_stack:
            return
        self._tree = self._undo_stack.pop()
        self._undo_button.set_sensitive(bool(self._undo_stack))
        self._drawing_area.queue_draw()

    def _reset_tree(self):
        """Replace the tree with a single leaf, pushing the prior state to undo."""
        self._push_undo()
        self._tree = SplitTree.new_leaf()
        self._drawing_area.queue_draw()

    @property
    def split_tree(self):
        return self._tree

    def _on_draw(self, widget, cr):
        w = self.CANVAS_WIDTH
        h = self._canvas_height

        # Draw background
        cr.set_source_rgb(0.2, 0.2, 0.2)
        cr.rectangle(0, 0, w, h)
        cr.fill()

        # Draw split regions
        self._draw_regions(cr, w, h)

        # Outer border
        cr.set_source_rgb(0, 0, 0)
        cr.set_line_width(2)
        cr.rectangle(0, 0, w, h)
        cr.stroke()

    def _draw_regions(self, cr, canvas_w, canvas_h):
        color_idx = [0]

        def draw_node(node, x, y, w, h):
            if node.is_leaf:
                ci = color_idx[0] % len(SPLIT_COLORS)
                cr.set_source_rgba(*SPLIT_COLORS[ci], 0.7)
                cr.rectangle(x, y, w, h)
                cr.fill()
                cr.set_source_rgb(0, 0, 0)
                cr.set_line_width(1)
                cr.rectangle(x, y, w, h)
                cr.stroke()
                color_idx[0] += 1
                return

            pct = int(round(node.proportion * 100))
            label = "%d/%d" % (pct, 100 - pct)

            if node.direction == 'V':
                left_w = w * node.proportion
                draw_node(node.left, x, y, left_w, h)
                draw_node(node.right, x + left_w, y, w - left_w, h)
                # Draw split line
                cr.set_source_rgb(1, 1, 1)
                cr.set_line_width(2)
                sx = x + left_w
                cr.move_to(sx, y)
                cr.line_to(sx, y + h)
                cr.stroke()
                # Draw percentage label
                cr.set_font_size(10)
                extents = cr.text_extents(label)
                lx = sx - extents.width / 2
                ly = y + h / 2 + extents.height / 2
                cr.set_source_rgba(0, 0, 0, 0.7)
                cr.rectangle(lx - 2, ly - extents.height - 2,
                             extents.width + 4, extents.height + 4)
                cr.fill()
                cr.set_source_rgb(1, 1, 1)
                cr.move_to(lx, ly)
                cr.show_text(label)
            else:  # 'H'
                top_h = h * node.proportion
                draw_node(node.left, x, y, w, top_h)
                draw_node(node.right, x, y + top_h, w, h - top_h)
                cr.set_source_rgb(1, 1, 1)
                cr.set_line_width(2)
                sy = y + top_h
                cr.move_to(x, sy)
                cr.line_to(x + w, sy)
                cr.stroke()
                # Draw percentage label
                cr.set_font_size(10)
                extents = cr.text_extents(label)
                lx = x + w / 2 - extents.width / 2
                ly = sy + extents.height / 2
                cr.set_source_rgba(0, 0, 0, 0.7)
                cr.rectangle(lx - 2, ly - extents.height - 2,
                             extents.width + 4, extents.height + 4)
                cr.fill()
                cr.set_source_rgb(1, 1, 1)
                cr.move_to(lx, ly)
                cr.show_text(label)

        draw_node(self._tree, 0, 0, canvas_w, canvas_h)

    SNAP_PERCENT = 5  # snap to nearest N%

    def _px_to_prop(self, px_x, px_y):
        """Convert pixel coordinates to proportional 0.0-1.0 space."""
        return px_x / self.CANVAS_WIDTH, px_y / self._canvas_height

    def _snap(self, prop):
        """Snap a proportion to the nearest SNAP_PERCENT increment, clamped to [0.1, 0.9]."""
        step = self.SNAP_PERCENT / 100.0
        snapped = round(prop / step) * step
        return max(0.1, min(0.9, snapped))

    GRAB_RADIUS_PX = 8  # how close (in canvas pixels) you have to click to grab an edge

    def _find_edge_at(self, event_x, event_y):
        px, py = self._px_to_prop(event_x, event_y)
        return self._tree.find_nearest_edge(
            px, py,
            threshold_px=self.GRAB_RADIUS_PX,
            canvas_w=self.CANVAS_WIDTH, canvas_h=self._canvas_height,
        )

    def _on_button_press(self, widget, event):
        if event.button == 3:
            # Right-click: remove nearest edge.  Convert that subtree
            # back to a single leaf — discards both children.  The
            # earlier "promote-left-subtree" implementation preserved
            # nested splits when removing an outer edge, which was
            # confusing; users right-clicking a line expect that
            # entire split level to vanish, not for a child split to
            # take its place.
            edge = self._find_edge_at(event.x, event.y)
            if edge:
                node, parent, is_left, dist = edge
                self._push_undo()
                node.direction = None
                node.proportion = 0.5
                node.left = None
                node.right = None
                node.primary = False
                self._drawing_area.queue_draw()
            return

        if event.button == 1:
            self._mouse_down_at = (event.x, event.y)
            self._drag_decision = None

            # Check if near an existing edge to move it
            edge = self._find_edge_at(event.x, event.y)
            if edge:
                self._drag_mode = 'move_edge'
                self._drag_target_edge = edge[0]  # the split node
                # Snapshot before resize starts (motion events will mutate
                # node.proportion in place; pushing once at drag start
                # gives the user a single Undo to revert the whole drag).
                self._push_undo()
                return

            # Otherwise, prepare for new split.  Undo snapshot is taken
            # later in the motion handler when the drag actually
            # crosses the threshold and the leaf gets converted into
            # a split — clicking without dragging mustn't pollute the
            # undo stack.
            px, py = self._px_to_prop(event.x, event.y)
            self._drag_mode = 'new_split'
            result = self._tree.get_split_for_point(px, py)
            self._drag_target_leaf = result[0]  # the leaf node

    def _on_button_release(self, widget, event):
        self._mouse_down_at = None
        self._drag_mode = None
        self._drag_target_leaf = None
        self._drag_target_edge = None
        self._drag_decision = None

    def _on_motion(self, widget, event):
        if not self._mouse_down_at:
            # Hover feedback: change cursor when over a draggable edge.
            self._update_hover_cursor(event.x, event.y)
            return

        if self._drag_mode == 'move_edge':
            node = self._drag_target_edge
            if node is None:
                return
            px, py = self._px_to_prop(event.x, event.y)
            region = self._tree.find_node_region(node)
            if node.direction == 'V':
                if region:
                    rx, ry, rw, rh = region
                    new_prop = (px - rx) / rw if rw > 0 else 0.5
                else:
                    new_prop = px
                node.proportion = self._snap(new_prop)
            else:
                if region:
                    rx, ry, rw, rh = region
                    new_prop = (py - ry) / rh if rh > 0 else 0.5
                else:
                    new_prop = py
                node.proportion = self._snap(new_prop)
            self._drawing_area.queue_draw()
            return

        if self._drag_mode == 'new_split':
            leaf = self._drag_target_leaf
            if leaf is None:
                return

            xdiff = abs(event.x - self._mouse_down_at[0])
            ydiff = abs(event.y - self._mouse_down_at[1])

            threshold = 20  # pixels

            if self._drag_decision is None:
                if xdiff > threshold and xdiff > ydiff:
                    self._drag_decision = 'H'  # horizontal drag = vertical split line
                elif ydiff > threshold:
                    self._drag_decision = 'V'  # vertical drag = horizontal split line
                if self._drag_decision is not None:
                    # Snapshot the tree before turning this leaf into a
                    # split.  One push covers the whole drag.
                    self._push_undo()

            if self._drag_decision is not None:
                px, py = self._px_to_prop(event.x, event.y)
                # Find the leaf's region
                result = self._tree.get_split_for_point(px, py)
                _, rx, ry, rw, rh = result

                if self._drag_decision == 'H':
                    # Vertical split line (drag horizontal)
                    prop = (px - rx) / rw if rw > 0 else 0.5
                    leaf.direction = 'V'
                    leaf.proportion = self._snap(prop)
                    leaf.left = SplitTree.new_leaf()
                    leaf.right = SplitTree.new_leaf()
                else:
                    # Horizontal split line (drag vertical)
                    prop = (py - ry) / rh if rh > 0 else 0.5
                    leaf.direction = 'H'
                    leaf.proportion = self._snap(prop)
                    leaf.left = SplitTree.new_leaf()
                    leaf.right = SplitTree.new_leaf()

                # After creating the split, switch to move mode for fine-tuning
                self._drag_mode = 'move_edge'
                self._drag_target_edge = leaf
                self._drag_target_leaf = None

                self._drawing_area.queue_draw()

    def _update_hover_cursor(self, ex, ey):
        edge = self._find_edge_at(ex, ey)
        win = self._drawing_area.get_window()
        if not win:
            return
        if edge:
            display = win.get_display()
            cursor_name = 'col-resize' if edge[0].direction == 'V' else 'row-resize'
            cursor = Gdk.Cursor.new_from_name(display, cursor_name)
            win.set_cursor(cursor)
        else:
            win.set_cursor(None)

