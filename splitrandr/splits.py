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

    @staticmethod
    def __new_leaf():
        t = SplitTree.__new__(SplitTree)
        t.direction = None
        t.proportion = 0.5
        t.left = None
        t.right = None
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

    def find_nearest_edge(self, px, py, x=0.0, y=0.0, w=1.0, h=1.0, threshold=0.03):
        """Find the nearest split edge to point (px, py) in proportional space.
        Returns (node, parent, is_left_child, distance) or None."""
        results = []
        self._collect_edges(px, py, x, y, w, h, None, True, results)
        if not results:
            return None
        results.sort(key=lambda r: r[3])
        if results[0][3] < threshold:
            return results[0]
        return None

    def _collect_edges(self, px, py, x, y, w, h, parent, is_left, results):
        if self.is_leaf:
            return

        if self.direction == 'V':
            edge_x = x + w * self.proportion
            if y <= py <= y + h:
                dist = abs(px - edge_x)
                results.append((self, parent, is_left, dist))
            self.left._collect_edges(px, py, x, y, w * self.proportion, h, self, True, results)
            self.right._collect_edges(
                px, py, edge_x, y, w * (1 - self.proportion), h, self, False, results)
        else:
            edge_y = y + h * self.proportion
            if x <= px <= x + w:
                dist = abs(py - edge_y)
                results.append((self, parent, is_left, dist))
            self.left._collect_edges(px, py, x, y, w, h * self.proportion, self, True, results)
            self.right._collect_edges(
                px, py, x, edge_y, w, h * (1 - self.proportion), self, False, results)

    def to_setmonitor_commands(self, output_name, width, height, x_off, y_off, w_mm, h_mm):
        """Generate xrandr --setmonitor argument lists.
        Returns list of (monitor_name, geometry_str, output_or_none).
        First sub-monitor gets the real output name, rest get 'none'.
        """
        regions = list(self.leaf_regions(width, height, x_off, y_off, w_mm, h_mm))
        commands = []
        for i, (rx, ry, rw, rh, rmm_w, rmm_h) in enumerate(regions):
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

    def copy(self):
        if self.is_leaf:
            return SplitTree.new_leaf()
        return SplitTree(self.direction, self.proportion,
                         self.left.copy(), self.right.copy())


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

        content = self.get_content_area()

        label = Gtk.Label()
        label.set_markup(
            "<small>Drag to split. Right-click a split line to remove.</small>"
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

        content.show_all()

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

    def _on_button_press(self, widget, event):
        px, py = self._px_to_prop(event.x, event.y)

        if event.button == 3:
            # Right-click: remove nearest edge
            edge = self._tree.find_nearest_edge(px, py, threshold=0.05)
            if edge:
                node, parent, is_left, dist = edge
                # Replace node with its left child (promote left subtree)
                node.direction = node.left.direction
                node.proportion = node.left.proportion
                old_left = node.left
                node.right = old_left.right
                node.left = old_left.left
                self._drawing_area.queue_draw()
            return

        if event.button == 1:
            self._mouse_down_at = (event.x, event.y)
            self._drag_decision = None

            # Check if near an existing edge to move it
            edge = self._tree.find_nearest_edge(px, py, threshold=0.04)
            if edge:
                self._drag_mode = 'move_edge'
                self._drag_target_edge = edge[0]  # the split node
                return

            # Otherwise, prepare for new split
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
            return

        if self._drag_mode == 'move_edge':
            node = self._drag_target_edge
            if node is None:
                return
            px, py = self._px_to_prop(event.x, event.y)
            region = self._find_node_region(self._tree, node, 0.0, 0.0, 1.0, 1.0)
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

    def _find_node_region(self, current, target, x, y, w, h):
        """Find the (x, y, w, h) region of a target node within the tree."""
        if current is target:
            return (x, y, w, h)
        if current.is_leaf:
            return None

        if current.direction == 'V':
            left_w = w * current.proportion
            result = self._find_node_region(current.left, target, x, y, left_w, h)
            if result:
                return result
            return self._find_node_region(
                current.right, target, x + left_w, y, w - left_w, h)
        else:
            top_h = h * current.proportion
            result = self._find_node_region(current.left, target, x, y, w, top_h)
            if result:
                return result
            return self._find_node_region(
                current.right, target, x, y + top_h, w, h - top_h)
