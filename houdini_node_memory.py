"""
Houdini Node Library Manager
基于文件系统的节点组预设库，支持分类、保存、更新、放置、重命名、删除、拖拽移动、固定面板、备注、撤销/恢复。
- 修复：自动清理旧数据中 file 字段的 .cpio 后缀，避免路径重复扩展名。
- 组为叶子节点，不能作为文件夹接受其他项。
- 撤销/恢复最多保留 20 步操作。
将完整脚本复制到 Python Panel 或保存为 .py 文件后在 Houdini 中执行 show_node_library() 即可打开面板。
"""

import os
import json
import uuid
import shutil
import re
from datetime import datetime

import hou
from PySide2 import QtWidgets, QtCore, QtGui



# ============================================================
# 撤销 / 恢复 命令
# ============================================================
class UndoCommand:
    def undo(self):
        raise NotImplementedError
    def redo(self):
        raise NotImplementedError

class UndoStack:
    def __init__(self, max_steps=20):
        self._undo_stack = []
        self._redo_stack = []
        self._max_steps = max_steps

    def push(self, command):
        self._undo_stack.append(command)
        if len(self._undo_stack) > self._max_steps:
            self._undo_stack.pop(0)
        self._redo_stack.clear()

    def undo(self):
        if not self._undo_stack:
            return False
        cmd = self._undo_stack.pop()
        try:
            cmd.undo()
            self._redo_stack.append(cmd)
            return True
        except Exception as e:
            hou.ui.displayMessage(f"撤销失败：{e}", severity=hou.severityType.Error)
            return False

    def redo(self):
        if not self._redo_stack:
            return False
        cmd = self._redo_stack.pop()
        try:
            cmd.redo()
            self._undo_stack.append(cmd)
            return True
        except Exception as e:
            hou.ui.displayMessage(f"恢复失败：{e}", severity=hou.severityType.Error)
            return False

    def can_undo(self):
        return len(self._undo_stack) > 0
    def can_redo(self):
        return len(self._redo_stack) > 0


class CreateFolderCommand(UndoCommand):
    def __init__(self, panel, parent_path, folder_name):
        self.panel = panel
        self.parent_path = parent_path
        self.folder_name = folder_name
        self.full_path = os.path.join(parent_path, folder_name)

    def redo(self):
        os.makedirs(self.full_path, exist_ok=True)

    def undo(self):
        if os.path.exists(self.full_path):
            shutil.rmtree(self.full_path)


class SaveGroupCommand(UndoCommand):
    def __init__(self, panel, folder_path, group_meta, cpio_filepath):
        self.panel = panel
        self.folder_path = folder_path
        self.group_meta = group_meta
        self.cpio_filepath = cpio_filepath

    def redo(self):
        meta = self.panel._load_meta(self.folder_path)
        meta["groups"].append(self.group_meta)
        self.panel._save_meta(self.folder_path, meta)
        print(f"DEBUG SaveGroupCommand.redo():")
        print(f"  folder_path: {self.folder_path}")
        print(f"  cpio_filepath: {self.cpio_filepath}")
        print(f"  group_meta['file']: {self.group_meta['file']}")

    def undo(self):
        # 删除复制到目标文件夹的 cpio 文件
        dst_path = os.path.join(self.folder_path, self.group_meta["file"] + ".cpio")
        if os.path.exists(dst_path):
            os.remove(dst_path)
        meta = self.panel._load_meta(self.folder_path)
        meta["groups"] = [g for g in meta["groups"] if g["file"] != self.group_meta["file"]]
        self.panel._save_meta(self.folder_path, meta)


class DeleteItemCommand(UndoCommand):
    def __init__(self, panel, item_type, backup_info):
        self.panel = panel
        self.item_type = item_type
        self.backup_info = backup_info
        self.trash_dir = os.path.join(panel.root_path, ".trash")
        self.trash_path = None
        os.makedirs(self.trash_dir, exist_ok=True)

    def redo(self):
        if self.item_type == "group":
            info = self.backup_info
            if os.path.exists(info["cpio_src"]):
                dst = os.path.join(self.trash_dir, info["file"] + ".cpio")
                shutil.move(info["cpio_src"], dst)
                self.trash_path = dst
            folder = info["folder"]
            meta = self.panel._load_meta(folder)
            meta["groups"] = [g for g in meta["groups"] if g["file"] != info["file"]]
            self.panel._save_meta(folder, meta)
        else:
            info = self.backup_info
            if os.path.exists(info["full_path"]):
                dst = os.path.join(self.trash_dir, info["name"])
                if os.path.exists(dst):
                    shutil.rmtree(dst)
                shutil.move(info["full_path"], dst)
                self.trash_path = dst

    def undo(self):
        if self.item_type == "group":
            info = self.backup_info
            src = self.trash_path or os.path.join(self.trash_dir, info["file"] + ".cpio")
            dst = info["cpio_src"]
            if os.path.exists(src) and not os.path.exists(dst):
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.move(src, dst)
            folder = info["folder"]
            meta = self.panel._load_meta(folder)
            if not any(g["file"] == info["file"] for g in meta["groups"]):
                meta["groups"].append(info["meta"])
            self.panel._save_meta(folder, meta)
        else:
            info = self.backup_info
            src = self.trash_path or os.path.join(self.trash_dir, info["name"])
            dst = info["full_path"]
            if os.path.exists(src) and not os.path.exists(dst):
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.move(src, dst)


class RenameCommand(UndoCommand):
    def __init__(self, panel, item_type, old_name, new_name, folder_path, file_id=None):
        self.panel = panel
        self.item_type = item_type
        self.old_name = old_name
        self.new_name = new_name
        self.folder_path = folder_path
        self.file_id = file_id

    def redo(self):
        if self.item_type == "group":
            meta = self.panel._load_meta(self.folder_path)
            for g in meta["groups"]:
                if g["file"] == self.file_id:
                    g["name"] = self.new_name
                    break
            self.panel._save_meta(self.folder_path, meta)
        else:
            old_path = os.path.join(self.folder_path, self.old_name)
            new_path = os.path.join(self.folder_path, self.new_name)
            os.rename(old_path, new_path)

    def undo(self):
        if self.item_type == "group":
            meta = self.panel._load_meta(self.folder_path)
            for g in meta["groups"]:
                if g["file"] == self.file_id:
                    g["name"] = self.old_name
                    break
            self.panel._save_meta(self.folder_path, meta)
        else:
            new_path = os.path.join(self.folder_path, self.new_name)
            old_path = os.path.join(self.folder_path, self.old_name)
            os.rename(new_path, old_path)


class MoveGroupCommand(UndoCommand):
    def __init__(self, panel, old_folder, old_file, old_index, new_folder, new_file, new_index):
        self.panel = panel
        self.old_folder = old_folder
        self.old_file = old_file
        self.old_index = old_index
        self.new_folder = new_folder
        self.new_file = new_file
        self.new_index = new_index
        self._meta_entry = None

    def redo(self):
        old_meta = self.panel._load_meta(self.old_folder)
        for g in old_meta["groups"]:
            if g["file"] == self.old_file:
                self._meta_entry = dict(g)
                break

        src_file = os.path.join(self.old_folder, self.old_file + ".cpio")
        if not os.path.exists(src_file):
            print(f"DEBUG: Source file not found at {src_file}, searching...")
            found_path = self._find_cpio_file(self.old_file)
            if found_path:
                self.old_folder = os.path.dirname(found_path)
                print(f"DEBUG: Found file at {found_path}")
                old_meta = self.panel._load_meta(self.old_folder)
                for g in old_meta["groups"]:
                    if g["file"] == self.old_file:
                        self._meta_entry = dict(g)
                        break
            else:
                print(f"DEBUG: File {self.old_file}.cpio not found anywhere!")

        old_meta["groups"] = [g for g in old_meta["groups"] if g["file"] != self.old_file]
        self.panel._save_meta(self.old_folder, old_meta)

        if self.old_folder != self.new_folder:
            src = os.path.join(self.old_folder, self.old_file + ".cpio")
            dst = os.path.join(self.new_folder, self.new_file + ".cpio")
            print(f"DEBUG MoveGroupCommand.redo():")
            print(f"  old_folder: {self.old_folder}")
            print(f"  new_folder: {self.new_folder}")
            print(f"  old_file: {self.old_file}")
            print(f"  src: {src}")
            print(f"  dst: {dst}")
            print(f"  Source exists: {os.path.exists(src)}")
            if os.path.exists(src):
                shutil.move(src, dst)
                print(f"  File moved successfully")
            else:
                print(f"  ERROR: Source file does not exist!")
        else:
            print(f"DEBUG MoveGroupCommand.redo(): Same folder, no file move")

        new_meta = self.panel._load_meta(self.new_folder)
        if self._meta_entry:
            new_meta["groups"].append(self._meta_entry)
        self.panel._save_meta(self.new_folder, new_meta)

    def _find_cpio_file(self, file_id):
        for root, dirs, files in os.walk(self.panel.root_path):
            if ".trash" in root:
                continue
            for f in files:
                if f == file_id + ".cpio":
                    return os.path.join(root, f)
        return None

    def undo(self):
        new_meta = self.panel._load_meta(self.new_folder)
        new_meta["groups"] = [g for g in new_meta["groups"] if g["file"] != self.new_file]
        self.panel._save_meta(self.new_folder, new_meta)

        if self.old_folder != self.new_folder:
            src = os.path.join(self.new_folder, self.new_file + ".cpio")
            dst = os.path.join(self.old_folder, self.old_file + ".cpio")
            if os.path.exists(src):
                shutil.move(src, dst)

        old_meta = self.panel._load_meta(self.old_folder)
        old_meta["groups"].append(self._meta_entry)
        self.panel._save_meta(self.old_folder, old_meta)


class MoveFolderCommand(UndoCommand):
    def __init__(self, panel, old_parent, old_name, new_parent, new_name):
        self.panel = panel
        self.old_parent = old_parent
        self.old_name = old_name
        self.new_parent = new_parent
        self.new_name = new_name
        self.old_path = os.path.join(old_parent, old_name)
        self.new_path = os.path.join(new_parent, new_name)

    def redo(self):
        if os.path.exists(self.old_path):
            shutil.move(self.old_path, self.new_path)

    def undo(self):
        if os.path.exists(self.new_path):
            shutil.move(self.new_path, self.old_path)


# ============================================================
# 自定义 QTreeWidget 子类，处理拖拽事件
class NodeTreeWidget(QtWidgets.QTreeWidget):
    def __init__(self, panel):
        super().__init__()
        self.panel = panel
    
    def dragEnterEvent(self, event):
        print(f"DEBUG dragEnterEvent(): Event received")
        if event.mimeData().hasFormat('application/x-qabstractitemmodeldatalist'):
            event.accept()
        else:
            event.ignore()
    
    def dragMoveEvent(self, event):
        if event.mimeData().hasFormat('application/x-qabstractitemmodeldatalist'):
            event.setDropAction(QtCore.Qt.MoveAction)
            event.accept()
        else:
            event.ignore()
    
    def dropEvent(self, event):
        print(f"DEBUG dropEvent(): Event received")
        source_item = self.currentItem()
        print(f"DEBUG dropEvent(): source_item = {source_item}")
        if not source_item:
            print(f"DEBUG dropEvent(): No source item, ignoring")
            event.ignore()
            return

        target_item = self.itemAt(event.pos())
        drop_indicator = self.dropIndicatorPosition()

        source_data = source_item.data(0, QtCore.Qt.UserRole)
        print(f"DEBUG dropEvent(): source_data = {source_data}")
        if not source_data:
            print(f"DEBUG dropEvent(): No source data, ignoring")
            event.ignore()
            return

        if target_item:
            target_data = target_item.data(0, QtCore.Qt.UserRole)
            if target_data and target_data["type"] == "group" and drop_indicator == self.OnItem:
                drop_indicator = self.BelowItem
        else:
            target_data = None

        new_parent_path = self.panel.root_path
        target_parent_item = None
        target_index = -1

        if target_item:
            target_data = target_item.data(0, QtCore.Qt.UserRole)
            if target_data and target_data["type"] == "folder":
                # 拖入文件夹，放入文件夹内部
                new_parent_path = target_data["path"]
                # 计算在文件夹内的位置
                new_index = len(self.panel._get_children_order(new_parent_path))
            else:
                target_parent_item = target_item.parent()
                if target_parent_item is None:
                    target_parent_item = self.invisibleRootItem()
                    new_parent_path = self.panel.root_path
                else:
                    parent_data = target_parent_item.data(0, QtCore.Qt.UserRole)
                    if parent_data and parent_data["type"] == "folder":
                        new_parent_path = parent_data["path"]
                    else:
                        new_parent_path = self.panel.root_path

                target_index = target_parent_item.indexOfChild(target_item)
                if drop_indicator == self.AboveItem:
                    pass
                elif drop_indicator == self.BelowItem:
                    target_index += 1
        else:
            new_parent_path = self.panel.root_path

        print(f"DEBUG dropEvent():")

        if source_data["type"] == "group":
            old_file = source_data["file"]
            old_folder = source_data["folder"]
            print(f"  source_data['folder']: {old_folder}")
            print(f"  new_parent_path: {new_parent_path}")
            old_order = self.panel._get_children_order(old_folder)
            old_index = next((i for i, e in enumerate(old_order) if e["type"]=="group" and e["file"]==old_file), -1)

            if target_data and target_data["type"]=="folder":
                # 放入文件夹内部，使用之前计算的 new_index
                pass
            else:
                new_index = target_index
                if new_index < 0:
                    new_index = len(self.panel._get_children_order(new_parent_path))
                if old_folder == new_parent_path and new_index > old_index:
                    new_index -= 1
            cmd = MoveGroupCommand(self.panel, old_folder, old_file, old_index,
                                   new_parent_path, old_file, new_index)
        elif source_data["type"] == "folder":
            old_name = os.path.basename(source_data["path"])
            old_parent = os.path.dirname(source_data["path"])
            new_name = old_name
            if new_parent_path == old_parent:
                event.ignore()
                return
            cmd = MoveFolderCommand(self.panel, old_parent, old_name, new_parent_path, new_name)
        else:
            event.ignore()
            return

        try:
            cmd.redo()
            self.panel.undo_stack.push(cmd)
            event.accept()

            saved_expanded = self.panel._save_expanded_state()
            self.panel.refresh_tree()
            self.panel._restore_expanded_state(saved_expanded)
        except Exception as e:
            hou.ui.displayMessage(f"移动失败：{e}", severity=hou.severityType.Error)
            event.ignore()

# 主面板
# ============================================================
class NodeLibraryPanel(QtWidgets.QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Node Library")
        self.resize(700, 500)

        self.root_path = os.path.join(hou.homeHoudiniDirectory(), "node_library")
        self._ensure_root_exists()

        self.undo_stack = UndoStack(max_steps=20)

        self._current_note_item = None
        self._note_dirty = False

        self._init_ui()
        self.refresh_tree()
        
        saved_expanded = self._load_initial_expanded_state()
        self._restore_expanded_state(saved_expanded)

        self.btn_new_folder.clicked.connect(self.create_folder)
        self.btn_save.clicked.connect(self.save_selected)
        self.btn_update.clicked.connect(self.update_group)
        self.btn_place.clicked.connect(self.place_group)
        self.tree.customContextMenuRequested.connect(self.on_context_menu)
        self.tree.currentItemChanged.connect(self.on_tree_selection_changed)
        self.note_edit.textChanged.connect(self._on_note_text_changed)

    # ---------- UI ----------
    def _init_ui(self):
        main_layout = QtWidgets.QHBoxLayout(self)

        self.tree = NodeTreeWidget(self)
        self.tree.setHeaderHidden(False)
        self.tree.setHeaderLabels(["名称", "创建日期"])
        self.tree.header().resizeSection(0, 200)
        self.tree.header().resizeSection(1, 150)
        self.tree.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
        self.tree.setDragEnabled(True)
        self.tree.setAcceptDrops(True)
        self.tree.setDropIndicatorShown(True)
        self.tree.setDragDropMode(QtWidgets.QAbstractItemView.InternalMove)
        self.tree.setDefaultDropAction(QtCore.Qt.MoveAction)
        self.tree.setIconSize(QtCore.QSize(192, 192))
        main_layout.addWidget(self.tree, 1)

        right_widget = QtWidgets.QWidget()
        right_layout = QtWidgets.QVBoxLayout(right_widget)
        right_layout.setContentsMargins(5, 0, 0, 0)

        btn_new_folder = QtWidgets.QPushButton("新建文件夹")
        btn_save = QtWidgets.QPushButton("保存节点组")
        btn_update = QtWidgets.QPushButton("更新节点组")
        btn_place = QtWidgets.QPushButton("放置节点组")

        note_label = QtWidgets.QLabel("备注")
        note_edit = QtWidgets.QTextEdit()
        note_edit.setPlaceholderText("选择节点组后输入说明...")
        note_edit.setEnabled(False)

        right_layout.addWidget(btn_new_folder)
        right_layout.addWidget(btn_save)
        right_layout.addWidget(btn_update)
        right_layout.addWidget(btn_place)
        right_layout.addStretch()
        right_layout.addWidget(note_label)
        right_layout.addWidget(note_edit, 1)

        main_layout.addWidget(right_widget)

        status_bar = QtWidgets.QStatusBar()
        status_bar.showMessage("就绪")
        main_layout.addWidget(status_bar)

        self.btn_new_folder = btn_new_folder
        self.btn_save = btn_save
        self.btn_update = btn_update
        self.btn_place = btn_place
        self.note_edit = note_edit
        self.status_bar = status_bar

        self.tree.itemDoubleClicked.connect(self.on_item_double_clicked)
        self.tree.itemExpanded.connect(self.on_item_expanded_or_collapsed)
        self.tree.itemCollapsed.connect(self.on_item_expanded_or_collapsed)

        undo_shortcut = QtWidgets.QShortcut(QtGui.QKeySequence("Ctrl+Z"), self)
        undo_shortcut.activated.connect(self.undo)
        redo_shortcut = QtWidgets.QShortcut(QtGui.QKeySequence("Ctrl+Y"), self)
        redo_shortcut.activated.connect(self.redo)

        self.setWindowFlags(self.windowFlags() | QtCore.Qt.WindowStaysOnTopHint)

    def on_item_double_clicked(self, item, column):
        data = item.data(0, QtCore.Qt.UserRole)
        if data and data["type"] == "folder":
            item.setExpanded(not item.isExpanded())
        elif data and data["type"] == "group":
            self.place_group()

    def update_status_bar(self):
        total_groups = 0
        total_folders = 0

        def count_items(parent_item):
            nonlocal total_groups, total_folders
            for i in range(parent_item.childCount()):
                child = parent_item.child(i)
                data = child.data(0, QtCore.Qt.UserRole)
                if data:
                    if data["type"] == "group":
                        total_groups += 1
                    elif data["type"] == "folder":
                        total_folders += 1
                        count_items(child)

        count_items(self.tree.invisibleRootItem())
        self.status_bar.showMessage(f"共 {total_folders} 个分类，{total_groups} 个节点组")

    def on_item_expanded_or_collapsed(self):
        self._save_expanded_state_to_file()

    def _rebuild_all_orders(self):
        self._rebuild_order_recursive(self.tree.invisibleRootItem(), self.root_path)

    def _rebuild_order_recursive(self, parent_item, folder_path):
        self._rebuild_order_from_tree(parent_item, folder_path)
        for i in range(parent_item.childCount()):
            child = parent_item.child(i)
            data = child.data(0, QtCore.Qt.UserRole)
            if data and data.get("type") == "folder":
                self._rebuild_order_recursive(child, data["path"])

    # ---------- 撤销/恢复 ----------
    def undo(self):
        if self.undo_stack.undo():
            self.refresh_tree()

    def redo(self):
        if self.undo_stack.redo():
            self.refresh_tree()

    # ---------- 文件操作 ----------
    def _ensure_root_exists(self):
        os.makedirs(self.root_path, exist_ok=True)

    def _meta_path(self, folder):
        return os.path.join(folder, ".meta.json")

    def _load_meta(self, folder):
        path = self._meta_path(folder)
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, list):
                        data = {"groups": data, "order": []}
                    # ---- 修复旧数据：file 字段不能包含 .cpio ----
                    changed = False
                    for g in data.get("groups", []):
                        if g.get("file", "").endswith(".cpio"):
                            g["file"] = g["file"][:-5]
                            changed = True
                    if changed:
                        self._save_meta(folder, data)
                    return data
            except Exception:
                return {"groups": [], "order": []}
        return {"groups": [], "order": []}

    def _save_meta(self, folder, data):
        path = self._meta_path(folder)
        print(f"DEBUG _save_meta(): Saving to {path}")
        print(f"DEBUG _save_meta(): groups count = {len(data.get('groups', []))}")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        print(f"DEBUG _save_meta(): Saved successfully")

    def _get_children_order(self, folder):
        order = []
        groups_dict = {}
        meta_path = os.path.join(folder, ".meta.json")
        if os.path.exists(meta_path):
            try:
                with open(meta_path, "r", encoding="utf-8") as f:
                    meta = json.load(f)
                    for g in meta.get("groups", []):
                        groups_dict[g["file"]] = g
            except Exception:
                pass
        try:
            entries = sorted(os.listdir(folder))
        except PermissionError:
            entries = []
        for entry in entries:
            subdir = os.path.join(folder, entry)
            if os.path.isdir(subdir) and not entry.startswith("."):
                order.append({"type": "folder", "name": entry})
            elif entry.endswith(".cpio"):
                file_id = entry[:-5]
                if file_id in groups_dict:
                    order.append({"type": "group", "file": file_id})
        return order

    def _save_children_order(self, folder, order):
        meta = self._load_meta(folder)
        meta["order"] = order
        self._save_meta(folder, meta)

    def _rebuild_order_from_tree(self, parent_item, folder_path):
        order = []
        for i in range(parent_item.childCount()):
            child = parent_item.child(i)
            data = child.data(0, QtCore.Qt.UserRole)
            if not data:
                continue
            if data["type"] == "group":
                order.append({"type": "group", "file": data["file"]})
            elif data["type"] == "folder":
                order.append({"type": "folder", "name": os.path.basename(data["path"])})
        self._save_children_order(folder_path, order)

    # ---------- 树刷新 ----------
    def refresh_tree(self, select_last=True):
        saved_meta_ref = None
        if select_last:
            current = self.tree.currentItem()
            if current:
                data = current.data(0, QtCore.Qt.UserRole)
                if data and data["type"] == "group":
                    saved_meta_ref = (data["folder"], data["file"])

        expanded_paths = self._save_expanded_state()

        self.tree.clear()
        self._populate_tree(self.root_path, self.tree)

        if saved_meta_ref:
            folder, fname = saved_meta_ref
            self._select_item_by_file(folder, fname)

        self._restore_expanded_state(expanded_paths)
        self.update_status_bar()
        self._save_expanded_state_to_file()

    def _load_initial_expanded_state(self):
        return self._load_expanded_state_from_file()

    def _save_expanded_state(self):
        expanded_paths = set()

        def collect_expanded(parent_item, current_path):
            for i in range(parent_item.childCount()):
                child = parent_item.child(i)
                data = child.data(0, QtCore.Qt.UserRole)
                if data and data["type"] == "folder":
                    folder_path = data["path"]
                    if child.isExpanded():
                        expanded_paths.add(folder_path)
                    collect_expanded(child, folder_path)

        collect_expanded(self.tree.invisibleRootItem(), self.root_path)
        return expanded_paths

    def _restore_expanded_state(self, expanded_paths):
        def expand_items(parent_item):
            for i in range(parent_item.childCount()):
                child = parent_item.child(i)
                data = child.data(0, QtCore.Qt.UserRole)
                if data and data["type"] == "folder":
                    if data["path"] in expanded_paths:
                        child.setExpanded(True)
                    expand_items(child)

        expand_items(self.tree.invisibleRootItem())

    def _populate_tree(self, dir_path, parent_item):
        app_style = QtWidgets.QApplication.style()
        order = self._get_children_order(dir_path)
        meta = self._load_meta(dir_path)
        groups_dict = {g["file"]: g for g in meta["groups"]}
        processed_folders = set()
        processed_groups = set()

        for entry in order:
            if entry["type"] == "group":
                file_id = entry["file"]
                group_meta = groups_dict.get(file_id)
                if group_meta and file_id not in processed_groups:
                    item = QtWidgets.QTreeWidgetItem(parent_item)
                    item.setText(0, group_meta["name"])
                    if "created" in group_meta:
                        created_date = group_meta["created"].split(" ")[0]
                        item.setText(1, created_date)
                    item.setData(0, QtCore.Qt.UserRole, {
                        "type": "group",
                        "file": file_id,
                        "folder": dir_path,
                        "meta": group_meta
                    })
                    item.setIcon(0, app_style.standardIcon(QtWidgets.QStyle.SP_FileIcon))
                    item.setFlags(item.flags() | QtCore.Qt.ItemIsDragEnabled)
                    processed_groups.add(file_id)
            elif entry["type"] == "folder":
                folder_name = entry["name"]
                subdir = os.path.join(dir_path, folder_name)
                if os.path.isdir(subdir) and not folder_name.startswith("."):
                    if folder_name not in processed_folders:
                        folder_item = QtWidgets.QTreeWidgetItem(parent_item)
                        folder_item.setText(0, folder_name)
                        folder_item.setData(0, QtCore.Qt.UserRole, {
                            "type": "folder",
                            "path": subdir
                        })
                        folder_item.setIcon(0, app_style.standardIcon(QtWidgets.QStyle.SP_DirIcon))
                        folder_item.setFlags(folder_item.flags() | QtCore.Qt.ItemIsDragEnabled | QtCore.Qt.ItemIsDropEnabled)
                        self._populate_tree(subdir, folder_item)
                        processed_folders.add(folder_name)

        try:
            entries = sorted(os.listdir(dir_path))
        except PermissionError:
            entries = []

        for entry in entries:
            subdir = os.path.join(dir_path, entry)
            if os.path.isdir(subdir) and not entry.startswith(".") and entry not in processed_folders:
                folder_item = QtWidgets.QTreeWidgetItem(parent_item)
                folder_item.setText(0, entry)
                folder_item.setData(0, QtCore.Qt.UserRole, {
                    "type": "folder",
                    "path": subdir
                })
                folder_item.setIcon(0, app_style.standardIcon(QtWidgets.QStyle.SP_DirIcon))
                folder_item.setFlags(folder_item.flags() | QtCore.Qt.ItemIsDragEnabled | QtCore.Qt.ItemIsDropEnabled)
                self._populate_tree(subdir, folder_item)

        for entry in entries:
            if entry.endswith(".cpio"):
                file_id = entry[:-5]
                if file_id not in processed_groups and file_id in groups_dict:
                    g = groups_dict[file_id]
                    item = QtWidgets.QTreeWidgetItem(parent_item)
                    item.setText(0, g["name"])
                    if "created" in g:
                        created_date = g["created"].split(" ")[0]
                        item.setText(1, created_date)
                    item.setData(0, QtCore.Qt.UserRole, {
                        "type": "group",
                        "file": file_id,
                        "folder": dir_path,
                        "meta": g
                    })
                    item.setIcon(0, app_style.standardIcon(QtWidgets.QStyle.SP_FileIcon))
                    item.setFlags(item.flags() | QtCore.Qt.ItemIsDragEnabled)

    def _select_item_by_file(self, folder, filename):
        def _find_and_select(parent_item):
            for i in range(parent_item.childCount()):
                child = parent_item.child(i)
                data = child.data(0, QtCore.Qt.UserRole)
                if data and data["type"] == "group":
                    if data["folder"] == folder and data["file"] == filename:
                        self.tree.setCurrentItem(child)
                        return True
                elif data and data["type"] == "folder":
                    if _find_and_select(child):
                        return True
            return False
        _find_and_select(self.tree.invisibleRootItem())

    # ---------- 备注 ----------
    def _clear_note_ui(self):
        self._auto_save_note_if_dirty()
        self.note_edit.clear()
        self.note_edit.setEnabled(False)
        self._current_note_item = None
        self._note_dirty = False

    def on_tree_selection_changed(self, current, previous):
        self._auto_save_note_if_dirty()
        if not current:
            self._clear_note_ui()
            return
        data = current.data(0, QtCore.Qt.UserRole)
        if data and data["type"] == "group":
            desc = data["meta"].get("description", "")
            self.note_edit.setPlainText(desc)
            self.note_edit.setEnabled(True)
            self._current_note_item = (data["folder"], data["file"])
            self._note_dirty = False
        else:
            self._clear_note_ui()

    def _on_note_text_changed(self):
        if self._current_note_item:
            self._note_dirty = True

    def _auto_save_note_if_dirty(self):
        if not self._note_dirty or not self._current_note_item:
            return
        folder, file_id = self._current_note_item
        new_desc = self.note_edit.toPlainText()
        meta = self._load_meta(folder)
        for g in meta["groups"]:
            if g["file"] == file_id:
                g["description"] = new_desc
                break
        self._save_meta(folder, meta)
        
        def update_tree_items(parent_item):
            for i in range(parent_item.childCount()):
                child = parent_item.child(i)
                data = child.data(0, QtCore.Qt.UserRole)
                if data and data["type"] == "group":
                    if data["folder"] == folder and data["file"] == file_id:
                        data["meta"]["description"] = new_desc
                        child.setData(0, QtCore.Qt.UserRole, data)
                elif data and data["type"] == "folder":
                    update_tree_items(child)
        
        update_tree_items(self.tree.invisibleRootItem())
        self._note_dirty = False

    def closeEvent(self, event):
        self._auto_save_note_if_dirty()
        self._save_expanded_state_to_file()
        super().closeEvent(event)

    def _get_config_path(self):
        config_dir = os.path.join(hou.homeHoudiniDirectory(), "node_library")
        os.makedirs(config_dir, exist_ok=True)
        return os.path.join(config_dir, ".expanded_state.json")

    def _save_expanded_state_to_file(self):
        try:
            expanded_paths = self._save_expanded_state()
            config_path = self._get_config_path()
            with open(config_path, "w", encoding="utf-8") as f:
                json.dump(list(expanded_paths), f, ensure_ascii=False)
        except Exception as e:
            pass

    def _load_expanded_state_from_file(self):
        try:
            config_path = self._get_config_path()
            if os.path.exists(config_path):
                with open(config_path, "r", encoding="utf-8") as f:
                    expanded_paths = set(json.load(f))
                return expanded_paths
        except Exception:
            pass
        return set()

    # ---------- 工具 ----------
    def _selected_item_data(self):
        item = self.tree.currentItem()
        if item:
            return item.data(0, QtCore.Qt.UserRole)
        return None

    def _get_target_folder(self):
        data = self._selected_item_data()
        if data and data["type"] == "folder":
            return data["path"]
        return self.root_path

    # ---------- 操作 ----------
    def create_folder(self):
        parent_path = self._get_target_folder()
        name, ok = QtWidgets.QInputDialog.getText(self, "新建分类", "分类名称：")
        if ok and name.strip():
            new_folder = os.path.join(parent_path, name.strip())
            if os.path.exists(new_folder):
                hou.ui.displayMessage("同名分类已存在。", severity=hou.severityType.Warning)
                return
            cmd = CreateFolderCommand(self, parent_path, name.strip())
            cmd.redo()
            self.undo_stack.push(cmd)
            self.refresh_tree()

    def save_selected(self):
        sel = hou.selectedNodes()
        if not sel:
            hou.ui.displayMessage("请先选择要保存的节点。", severity=hou.severityType.Warning)
            return

        parent = sel[0].parent()
        for node in sel:
            if node.parent() != parent:
                hou.ui.displayMessage("所选节点不在同一网络层级，无法保存。", severity=hou.severityType.Error)
                return

        target_folder = self._get_target_folder()
        data = self._selected_item_data()
        if data and data["type"] != "folder":
            ret = hou.ui.displayMessage("当前选中为节点组，将保存至根目录。是否继续？",
                                        buttons=("是", "否"), default_choice=1)
            if ret != 0:
                return
            target_folder = self.root_path

        name, ok = QtWidgets.QInputDialog.getText(self, "保存节点组", "组名称：")
        if not ok or not name.strip():
            return

        uid = uuid.uuid4().hex[:8]
        filename = uid + ".cpio"
        filepath = os.path.join(target_folder, filename)

        try:
            parent.saveItemsToFile(sel, filepath)
        except Exception as e:
            hou.ui.displayMessage(f"保存失败：{e}", severity=hou.severityType.Error)
            return

        group_meta = {
            "name": name.strip(),
            "file": uid,
            "uuid": uid,
            "created": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "description": ""
        }
        cmd = SaveGroupCommand(self, target_folder, group_meta, filepath)
        cmd.redo()
        self.undo_stack.push(cmd)
        self.refresh_tree()
        hou.ui.displayMessage(f"组“{name}”保存成功。")

    def place_group(self):
        """放置节点组到当前网络编辑器光标位置。"""
        data = self._selected_item_data()
        if not data or data["type"] != "group":
            hou.ui.displayMessage("请先在库中选择一个节点组。", severity=hou.severityType.Warning)
            return
        file_path = os.path.join(data["folder"], data["file"] + ".cpio")
        if not os.path.exists(file_path):
            hou.ui.displayMessage("节点文件丢失，无法放置。", severity=hou.severityType.Error)
            return

        # 查找网络编辑器面板
        pane = hou.ui.paneTabOfType(hou.paneTabType.NetworkEditor)
        if not pane:
            hou.ui.displayMessage("未找到活动的网络编辑器。", severity=hou.severityType.Error)
            return

        target_net = pane.pwd()
        
        # 获取放置位置：使用 visibleBounds 获取视图中心
        print(f"DEBUG place_group():")
        cursor_pos = hou.Vector2(0, 0)
        found = False
        
        # 方法：使用 visibleBounds()
        try:
            bounds = pane.visibleBounds()
            print(f"  visibleBounds() 返回: {bounds}")
            print(f"  visibleBounds() 类型: {type(bounds)}")
            
            # 检查 bounds 是否有 min/max 属性或可索引
            min_x, min_y, max_x, max_y = 0, 0, 0, 0
            
            # 方式 1: 尝试直接访问属性
            if hasattr(bounds, 'minx'):
                min_x = bounds.minx
                min_y = bounds.miny
                max_x = bounds.maxx
                max_y = bounds.maxy
                print(f"  通过属性获取: min({min_x}, {min_y}), max({max_x}, {max_y})")
            # 方式 2: 尝试索引访问 [min_x, min_y, max_x, max_y]
            elif hasattr(bounds, '__getitem__'):
                min_x = bounds[0]
                min_y = bounds[1]
                max_x = bounds[2]
                max_y = bounds[3]
                print(f"  通过索引获取: min({min_x}, {min_y}), max({max_x}, {max_y})")
            # 方式 3: 尝试把对象转成字符串解析
            else:
                bounds_str = str(bounds)
                import re
                nums = re.findall(r'[-+]?\d*\.\d+|\d+', bounds_str)
                if len(nums) >= 4:
                    min_x = float(nums[0])
                    min_y = float(nums[1])
                    max_x = float(nums[2])
                    max_y = float(nums[3])
                    print(f"  通过字符串解析获取: min({min_x}, {min_y}), max({max_x}, {max_y})")
            
            # 计算中心
            center_x = (min_x + max_x) / 2
            center_y = (min_y + max_y) / 2
            cursor_pos = hou.Vector2(center_x, center_y)
            found = True
            print(f"  方法 1 (visibleBounds): 找到中心 {cursor_pos}")
        except Exception as e:
            print(f"  方法 1 异常: {e}")
            import traceback
            print(f"  方法 1 堆栈: {traceback.format_exc()}")
        
        # 如果方法 1 失败，尝试 allVisibleRects()
        if not found:
            try:
                rects = pane.allVisibleRects()
                print(f"  allVisibleRects() 返回: {rects}")
                if rects and len(rects) > 0:
                    # 使用第一个可见矩形的中心
                    rect = rects[0]
                    center_x = (rect[0].x() + rect[1].x()) / 2
                    center_y = (rect[0].y() + rect[1].y()) / 2
                    cursor_pos = hou.Vector2(center_x, center_y)
                    found = True
                    print(f"  方法 2 (allVisibleRects): 找到中心 {cursor_pos}")
            except Exception as e:
                print(f"  方法 2 异常: {e}")
        
        # 如果都失败了，使用默认位置
        if not found:
            cursor_pos = hou.Vector2(2, 2)
            print(f"  方法 3 (默认): 使用 {cursor_pos}")
        else:
            print(f"  最终放置位置: {cursor_pos} (节点组将被自动选中，你可以直接拖动)")
        
        try:

            # 加载节点并设置位置
            old_nodes = set(target_net.children())
            target_net.loadItemsFromFile(file_path)
            new_nodes = set(target_net.children())
            loaded_nodes = list(new_nodes - old_nodes)
            
            print(f"  加载了 {len(loaded_nodes)} 个节点")
            
            # 计算节点组的边界框，使节点组中心与点击位置对齐
            if loaded_nodes:
                # 先获取所有节点的原始位置
                original_positions = [node.position() for node in loaded_nodes]
                
                # 计算原始位置的中心点
                min_x = min(p.x() for p in original_positions)
                max_x = max(p.x() for p in original_positions)
                min_y = min(p.y() for p in original_positions)
                max_y = max(p.y() for p in original_positions)
                center_original = hou.Vector2((min_x + max_x) / 2, (min_y + max_y) / 2)
                
                # 计算偏移：将中心移到光标位置
                delta = cursor_pos - center_original
                
                # 应用偏移
                for i, node in enumerate(loaded_nodes):
                    new_pos = original_positions[i] + delta
                    node.setPosition(new_pos)
                    print(f"  节点 {i} ({node.name()}): 从 {original_positions[i]} 移到 {new_pos}")
                
                # 自动选中新添加的节点组，方便用户拖动调整
                hou.clearAllSelected()
                for node in loaded_nodes:
                    node.setSelected(True)
                
        except Exception as e:
            import traceback
            traceback.print_exc()
            hou.ui.displayMessage(f"放置失败：{e}", severity=hou.severityType.Error)
            return
        
        hou.ui.displayMessage(f"节点组“{data['meta']['name']}”放置完成。")

    def update_group(self):
        data = self._selected_item_data()
        if not data or data["type"] != "group":
            hou.ui.displayMessage("请先在库中选择要更新的组。", severity=hou.severityType.Warning)
            return
        sel = hou.selectedNodes()
        if not sel:
            hou.ui.displayMessage("请选择用于更新的节点。", severity=hou.severityType.Warning)
            return
        parent = sel[0].parent()
        for node in sel:
            if node.parent() != parent:
                hou.ui.displayMessage("所选节点不在同一网络层级。", severity=hou.severityType.Error)
                return
        ret = hou.ui.displayMessage(
            f"将用当前选中的 {len(sel)} 个节点覆盖组“{data['meta']['name']}”，是否继续？",
            buttons=("是", "否"), default_choice=1
        )
        if ret != 0:
            return
        file_path = os.path.join(data["folder"], data["file"] + ".cpio")
        try:
            parent.saveItemsToFile(sel, file_path)
        except Exception as e:
            hou.ui.displayMessage(f"更新失败：{e}", severity=hou.severityType.Error)
            return
        hou.ui.displayMessage("组更新成功。")

    def delete_item(self, item, data):
        if data["type"] == "group":
            msg = f"确定要删除节点组“{data['meta']['name']}”吗？此操作可恢复。"
            ret = hou.ui.displayMessage(msg, buttons=("删除", "取消"), default_choice=1)
            if ret != 0:
                return
            backup_info = {
                "folder": data["folder"],
                "file": data["file"],
                "meta": dict(data["meta"]),
                "cpio_src": os.path.join(data["folder"], data["file"] + ".cpio")
            }
            cmd = DeleteItemCommand(self, "group", backup_info)
            cmd.redo()
            self.undo_stack.push(cmd)
        elif data["type"] == "folder":
            msg = f"确定要删除分类“{item.text(0)}”及其内部所有节点组吗？此操作可恢复。"
            ret = hou.ui.displayMessage(msg, buttons=("删除", "取消"), default_choice=1)
            if ret != 0:
                return
            backup_info = {
                "parent": os.path.dirname(data["path"]),
                "name": os.path.basename(data["path"]),
                "full_path": data["path"]
            }
            cmd = DeleteItemCommand(self, "folder", backup_info)
            cmd.redo()
            self.undo_stack.push(cmd)
        self.refresh_tree(select_last=False)

    def on_context_menu(self, pos):
        item = self.tree.currentItem()
        if not item:
            menu = QtWidgets.QMenu()
            new_folder_action = menu.addAction("新建分类")
            menu.addSeparator()
            undo_action = menu.addAction("撤销 (Ctrl+Z)")
            redo_action = menu.addAction("恢复 (Ctrl+Y)")
            action = menu.exec_(self.tree.viewport().mapToGlobal(pos))
            if action == new_folder_action:
                self.create_folder()
            elif action == undo_action:
                self.undo()
            elif action == redo_action:
                self.redo()
            return

        data = item.data(0, QtCore.Qt.UserRole)
        menu = QtWidgets.QMenu()

        if data["type"] == "folder":
            new_group_action = menu.addAction("新建节点组")
            new_folder_action = menu.addAction("新建分类")
            menu.addSeparator()

        rename_action = menu.addAction("重命名")
        delete_action = menu.addAction("删除")
        menu.addSeparator()

        if data["type"] == "group":
            show_in_explorer_action = menu.addAction("在资源管理器中显示")
            copy_path_action = menu.addAction("复制路径")
        elif data["type"] == "folder":
            show_in_explorer_action = menu.addAction("在资源管理器中显示")
            copy_path_action = menu.addAction("复制路径")

        menu.addSeparator()
        undo_action = menu.addAction("撤销 (Ctrl+Z)")
        redo_action = menu.addAction("恢复 (Ctrl+Y)")
        menu.addSeparator()
        refresh_action = menu.addAction("刷新")

        action = menu.exec_(self.tree.viewport().mapToGlobal(pos))

        if action == rename_action:
            self.rename_item(item, data)
        elif action == delete_action:
            self.delete_item(item, data)
        elif data["type"] == "folder" and action == new_folder_action:
            self.create_folder()
        elif data["type"] == "folder" and action == new_group_action:
            self.save_selected_to_folder(data["path"])
        elif action == show_in_explorer_action:
            self.show_in_explorer(data)
        elif action == copy_path_action:
            self.copy_path_to_clipboard(data)
        elif action == undo_action:
            self.undo()
        elif action == redo_action:
            self.redo()
        elif action == refresh_action:
            self.refresh_tree()

    def save_selected_to_folder(self, target_folder):
        sel = hou.selectedNodes()
        if not sel:
            hou.ui.displayMessage("请先选择要保存的节点。", severity=hou.severityType.Warning)
            return

        parent = sel[0].parent()
        for node in sel:
            if node.parent() != parent:
                hou.ui.displayMessage("所选节点不在同一网络层级，无法保存。", severity=hou.severityType.Error)
                return

        name, ok = QtWidgets.QInputDialog.getText(self, "保存节点组", "组名称：")
        if not ok or not name.strip():
            return

        uid = uuid.uuid4().hex[:8]
        filename = uid + ".cpio"
        filepath = os.path.join(target_folder, filename)

        try:
            parent.saveItemsToFile(sel, filepath)
        except Exception as e:
            hou.ui.displayMessage(f"保存失败：{e}", severity=hou.severityType.Error)
            return

        group_meta = {
            "name": name.strip(),
            "file": uid,
            "uuid": uid,
            "created": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "description": ""
        }
        cmd = SaveGroupCommand(self, target_folder, group_meta, filepath)
        cmd.redo()
        self.undo_stack.push(cmd)
        self.refresh_tree()
        hou.ui.displayMessage(f"组“{name}”保存成功。")

    def show_in_explorer(self, data):
        if data["type"] == "group":
            path = os.path.join(data["folder"], data["file"] + ".cpio")
        else:
            path = data["path"]

        if os.path.exists(path):
            if os.name == 'nt':
                os.startfile(os.path.dirname(path))
            else:
                import subprocess
                subprocess.run(['open', os.path.dirname(path)])

    def copy_path_to_clipboard(self, data):
        if data["type"] == "group":
            path = os.path.join(data["folder"], data["file"] + ".cpio")
        else:
            path = data["path"]

        clipboard = QtWidgets.QApplication.clipboard()
        clipboard.setText(path)

    def rename_item(self, item, data):
        old_name = item.text(0)
        new_name, ok = QtWidgets.QInputDialog.getText(self, "重命名", "新名称：", text=old_name)
        if not ok or not new_name.strip():
            return
        if data["type"] == "folder":
            parent_dir = os.path.dirname(data["path"])
            cmd = RenameCommand(self, "folder", old_name, new_name.strip(), parent_dir)
        else:
            cmd = RenameCommand(self, "group", old_name, new_name.strip(), data["folder"], file_id=data["file"])
        cmd.redo()
        self.undo_stack.push(cmd)
        self.refresh_tree()




# ============================================================
# 显示函数
# ============================================================
_node_library_instance = None

def show_node_library():
    global _node_library_instance
    if _node_library_instance is not None:
        try:
            _node_library_instance.close()
        except:
            pass
    _node_library_instance = NodeLibraryPanel()
    _node_library_instance.show()

# 直接运行：
show_node_library()
