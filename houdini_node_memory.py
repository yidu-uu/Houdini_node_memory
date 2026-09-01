"""
Houdini Node Library Manager
基于文件系统的节点组预设库，支持分类、保存、更新、放置、重命名、删除、拖拽移动、固定面板、备注、撤销/恢复、截图保存。
- 修复：自动清理旧数据中 file 字段的 .cpio 后缀，避免路径重复扩展名。
- 组为叶子节点，不能作为文件夹接受其他项。
- 撤销/恢复最多保留 20 步操作。
- 截图：每个节点组可创建一张截图，以相对路径（与 .cpio 同名的 .png）保存在组文件夹内，并在备注栏右侧以正方形预览口展示，支持自由选区、窗口选择、全屏捕获。
将完整脚本复制到 Python Panel 或保存为 .py 文件后在 Houdini 中执行 show_node_library() 即可打开面板。
"""

import os
import json
import uuid
import shutil
import re
import zipfile
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

    def clear(self):
        self._undo_stack.clear()
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
        self.thumb_trash_path = None  # 组截图缩略图在回收目录中的路径
        os.makedirs(self.trash_dir, exist_ok=True)

    def redo(self):
        if self.item_type == "group":
            info = self.backup_info
            if os.path.exists(info["cpio_src"]):
                dst = os.path.join(self.trash_dir, info["file"] + ".cpio")
                shutil.move(info["cpio_src"], dst)
                self.trash_path = dst
            # 同时把关联的截图缩略图一并移入回收目录
            thumb_src = os.path.join(info["folder"], info["file"] + ".png")
            if os.path.exists(thumb_src):
                thumb_dst = os.path.join(self.trash_dir, info["file"] + ".png")
                shutil.move(thumb_src, thumb_dst)
                self.thumb_trash_path = thumb_dst
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
            # 恢复截图缩略图
            if self.thumb_trash_path:
                thumb_dst = os.path.join(info["folder"], info["file"] + ".png")
                if os.path.exists(self.thumb_trash_path) and not os.path.exists(thumb_dst):
                    os.makedirs(os.path.dirname(thumb_dst), exist_ok=True)
                    shutil.move(self.thumb_trash_path, thumb_dst)
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
            # 同步移动截图缩略图（若存在），保持相对路径一致
            thumb_src = os.path.join(self.old_folder, self.old_file + ".png")
            thumb_dst = os.path.join(self.new_folder, self.new_file + ".png")
            if os.path.exists(thumb_src):
                shutil.move(thumb_src, thumb_dst)
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
            # 还原截图缩略图位置
            thumb_src = os.path.join(self.new_folder, self.new_file + ".png")
            thumb_dst = os.path.join(self.old_folder, self.old_file + ".png")
            if os.path.exists(thumb_src):
                shutil.move(thumb_src, thumb_dst)

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


# ============================================================
# 可点击的标签，用于截图预览口
class ClickableLabel(QtWidgets.QLabel):
    """带 clicked 信号的 QLabel，用于在点击截图预览口时放大查看。"""
    clicked = QtCore.Signal()

    def mousePressEvent(self, event):
        if event.button() == QtCore.Qt.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)


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
        self._screenshot_overlay = None  # 截图覆盖层引用，防止被垃圾回收
        self._screenshot_target = None   # 当前截图对应的节点组数据
        self._search_text = ""           # 搜索关键词（空表示不过滤）

        self._init_ui()
        self.refresh_tree()
        
        saved_expanded = self._load_initial_expanded_state()
        self._restore_expanded_state(saved_expanded)

        self.btn_new_folder.clicked.connect(self.create_folder)
        self.btn_save.clicked.connect(self.save_selected)
        self.btn_update.clicked.connect(self.update_group)
        self.btn_place.clicked.connect(self.place_group)
        self.btn_screenshot.clicked.connect(self.capture_screenshot)
        self.btn_backup.clicked.connect(self.backup_library)
        self.btn_restore.clicked.connect(self.restore_library)
        self.tree.customContextMenuRequested.connect(self.on_context_menu)
        self.tree.currentItemChanged.connect(self.on_tree_selection_changed)
        self.note_edit.textChanged.connect(self._on_note_text_changed)
        self.thumb_label.customContextMenuRequested.connect(self.on_thumb_context_menu)
        self.thumb_label.clicked.connect(self._open_thumb_preview)
        self.search_edit.textChanged.connect(self.on_search_changed)

    # ---------- UI ----------
    def _init_ui(self):
        main_layout = QtWidgets.QHBoxLayout(self)

        # 左侧：搜索框 + 树
        left_widget = QtWidgets.QWidget()
        left_layout = QtWidgets.QVBoxLayout(left_widget)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(4)

        self.search_edit = QtWidgets.QLineEdit()
        self.search_edit.setPlaceholderText("搜索节点组（名称/备注）...")
        self.search_edit.setClearButtonEnabled(True)
        left_layout.addWidget(self.search_edit)

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
        left_layout.addWidget(self.tree, 1)

        main_layout.addWidget(left_widget, 1)

        right_widget = QtWidgets.QWidget()
        right_layout = QtWidgets.QVBoxLayout(right_widget)
        right_layout.setContentsMargins(5, 0, 0, 0)

        btn_new_folder = QtWidgets.QPushButton("新建文件夹")
        btn_save = QtWidgets.QPushButton("保存节点组")
        btn_update = QtWidgets.QPushButton("更新节点组")
        btn_place = QtWidgets.QPushButton("放置节点组")
        btn_screenshot = QtWidgets.QPushButton("截图")
        btn_backup = QtWidgets.QPushButton("备份")
        btn_restore = QtWidgets.QPushButton("恢复")

        note_label = QtWidgets.QLabel("备注")
        note_edit = QtWidgets.QTextEdit()
        note_edit.setPlaceholderText("选择节点组后输入说明...")
        note_edit.setEnabled(False)

        # 截图预览：正方形预览口，位于备注右侧
        self.thumb_size = 200  # 预览口边长，修改此处可调整预览大小
        thumb_label = ClickableLabel()
        thumb_label.setFixedSize(self.thumb_size, self.thumb_size)
        thumb_label.setAlignment(QtCore.Qt.AlignCenter)
        thumb_label.setStyleSheet(
            "border: 1px solid #555; background-color: #2b2b2b; color: #888;"
        )
        thumb_label.setToolTip("当前节点组的截图预览\n左键点击可放大查看\n右键可重新截图或删除")
        thumb_label.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)

        note_row = QtWidgets.QHBoxLayout()
        note_row.setSpacing(6)
        note_row.addWidget(note_edit, 1)
        note_row.addWidget(thumb_label, 0)

        right_layout.addWidget(btn_new_folder)
        right_layout.addWidget(btn_save)
        right_layout.addWidget(btn_update)
        right_layout.addWidget(btn_place)
        right_layout.addWidget(btn_screenshot)
        right_layout.addStretch()
        right_layout.addWidget(note_label)
        right_layout.addLayout(note_row, 1)

        # 库维护：备份 / 恢复（与节点组操作视觉分隔）
        sep = QtWidgets.QFrame()
        sep.setFrameShape(QtWidgets.QFrame.HLine)
        sep.setFrameShadow(QtWidgets.QFrame.Sunken)
        right_layout.addWidget(sep)
        backup_row = QtWidgets.QHBoxLayout()
        backup_row.setSpacing(6)
        backup_row.addWidget(btn_backup)
        backup_row.addWidget(btn_restore)
        right_layout.addLayout(backup_row)

        main_layout.addWidget(right_widget)

        status_bar = QtWidgets.QStatusBar()
        status_bar.showMessage("就绪")
        main_layout.addWidget(status_bar)

        self.btn_new_folder = btn_new_folder
        self.btn_save = btn_save
        self.btn_update = btn_update
        self.btn_place = btn_place
        self.btn_screenshot = btn_screenshot
        self.btn_backup = btn_backup
        self.btn_restore = btn_restore
        self.note_edit = note_edit
        self.thumb_label = thumb_label
        self.status_bar = status_bar

        self.tree.itemDoubleClicked.connect(self.on_item_double_clicked)
        self.tree.itemExpanded.connect(self.on_item_expanded_or_collapsed)
        self.tree.itemCollapsed.connect(self.on_item_expanded_or_collapsed)

        undo_shortcut = QtWidgets.QShortcut(QtGui.QKeySequence("Ctrl+Z"), self)
        undo_shortcut.activated.connect(self.undo)
        redo_shortcut = QtWidgets.QShortcut(QtGui.QKeySequence("Ctrl+Y"), self)
        redo_shortcut.activated.connect(self.redo)

        # 截图快捷键（可在 Houdini 中自行修改触发键位）
        screenshot_shortcut = QtWidgets.QShortcut(QtGui.QKeySequence("Ctrl+Shift+A"), self)
        screenshot_shortcut.activated.connect(self.capture_screenshot)

        # 搜索框聚焦快捷键
        search_shortcut = QtWidgets.QShortcut(QtGui.QKeySequence("Ctrl+F"), self)
        search_shortcut.activated.connect(self._focus_search)

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
        # 以 invisibleRootItem 作为根，使 _populate_tree 中的 parent_item 始终是 QTreeWidgetItem，
        # 从而支持 removeChild 剪枝（QTreeWidget 本身没有 removeChild）。
        self._populate_tree(self.root_path, self.tree.invisibleRootItem())

        if saved_meta_ref:
            folder, fname = saved_meta_ref
            self._select_item_by_file(folder, fname)

        self._restore_expanded_state(expanded_paths)
        self.update_status_bar()
        # 搜索模式下不持久化折叠状态，避免被过滤结果覆盖用户原有的展开偏好
        if not self._search_text.strip():
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
        """递归构建树；返回是否添加了任何在搜索模式下应显示的项。

        搜索模式下：仅显示名称/备注匹配的节点组，并保留其祖先文件夹；
        不含匹配项的文件夹会被剪除，避免显示大量空目录。
        """
        app_style = QtWidgets.QApplication.style()
        order = self._get_children_order(dir_path)
        meta = self._load_meta(dir_path)
        groups_dict = {g["file"]: g for g in meta["groups"]}
        processed_folders = set()
        processed_groups = set()
        added_any = False

        for entry in order:
            if entry["type"] == "group":
                file_id = entry["file"]
                group_meta = groups_dict.get(file_id)
                if group_meta and file_id not in processed_groups:
                    if self._matches_search(group_meta.get("name", ""), group_meta.get("description", "")):
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
                        added_any = True
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
                        child_added = self._populate_tree(subdir, folder_item)
                        # 搜索模式下剪除不含匹配项的空文件夹
                        if self._search_text.strip() and not child_added:
                            parent_item.removeChild(folder_item)
                        else:
                            added_any = True
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
                child_added = self._populate_tree(subdir, folder_item)
                if self._search_text.strip() and not child_added:
                    parent_item.removeChild(folder_item)
                else:
                    added_any = True

        for entry in entries:
            if entry.endswith(".cpio"):
                file_id = entry[:-5]
                if file_id not in processed_groups and file_id in groups_dict:
                    g = groups_dict[file_id]
                    if self._matches_search(g.get("name", ""), g.get("description", "")):
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
                        added_any = True

        return added_any

    # ---------- 搜索 ----------
    def _matches_search(self, name, description):
        """判断名称/备注是否命中当前搜索关键词（不区分大小写）。"""
        q = self._search_text.strip().lower()
        if not q:
            return True
        return q in (name or "").lower() or q in (description or "").lower()

    def _focus_search(self):
        """聚焦搜索框并选中已有文本，便于直接输入新的关键词。"""
        self.search_edit.setFocus()
        self.search_edit.selectAll()

    def on_search_changed(self, text):
        """搜索关键词变化时刷新树（过滤模式不保留选中项，避免误选）。"""
        self._auto_save_note_if_dirty()
        self._search_text = text
        self.refresh_tree(select_last=False)
        self.update_status_bar()

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
        self._clear_thumb()

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
            self._load_thumb(data)
        else:
            self._clear_note_ui()

    # ---------- 截图预览 ----------
    def _clear_thumb(self):
        """清空截图预览口，显示占位提示。"""
        self.thumb_label.clear()
        self.thumb_label.setText("无截图")
        self.thumb_label.setToolTip("当前节点组的截图预览\n左键点击可放大查看\n右键可重新截图或删除")

    def _thumb_path(self, data):
        """根据组数据解析截图文件的绝对路径（meta 中保存相对路径）。"""
        rel = data.get("meta", {}).get("thumbnail")
        if not rel:
            return None
        # 相对路径始终以组所在文件夹为基准，防止被外部路径注入
        return os.path.join(data["folder"], os.path.basename(rel))

    def _load_thumb(self, data):
        """加载并显示当前组的截图缩略图。"""
        path = self._thumb_path(data)
        if path and os.path.exists(path):
            pm = QtGui.QPixmap(path)
            if not pm.isNull():
                s = self.thumb_size
                self.thumb_label.setText("")
                self.thumb_label.setPixmap(pm.scaled(
                    s, s, QtCore.Qt.KeepAspectRatio, QtCore.Qt.SmoothTransformation))
                self.thumb_label.setToolTip(path)
                return
        self._clear_thumb()

    def _open_thumb_preview(self):
        """点击预览口后，弹出对话框放大显示当前组的截图。"""
        data = self._selected_item_data()
        if not data or data["type"] != "group":
            return
        path = self._thumb_path(data)
        if not path or not os.path.exists(path):
            return
        pm = QtGui.QPixmap(path)
        if pm.isNull():
            return

        # 弹窗显示，缩放到可用屏幕 80% 以内并保持等比
        dlg = QtWidgets.QDialog(self)
        dlg.setWindowTitle(data["meta"].get("name", "截图预览"))
        dlg.setModal(True)
        layout = QtWidgets.QVBoxLayout(dlg)

        screen = QtWidgets.QApplication.primaryScreen()
        avail = screen.availableGeometry()
        max_w = int(avail.width() * 0.8)
        max_h = int(avail.height() * 0.8)
        scaled = pm
        if pm.width() > max_w or pm.height() > max_h:
            scaled = pm.scaled(
                max_w, max_h,
                QtCore.Qt.KeepAspectRatio,
                QtCore.Qt.SmoothTransformation,
            )

        image_label = QtWidgets.QLabel()
        image_label.setAlignment(QtCore.Qt.AlignCenter)
        image_label.setPixmap(scaled)
        scroll = QtWidgets.QScrollArea()
        scroll.setWidget(image_label)
        scroll.setWidgetResizable(True)
        layout.addWidget(scroll, 1)

        info_label = QtWidgets.QLabel(f"尺寸：{pm.width()} × {pm.height()} px")
        layout.addWidget(info_label)

        dlg.resize(min(max_w, pm.width()) + 40, min(max_h, pm.height()) + 60)
        dlg.exec_()

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

    # ---------- 备份 / 恢复 ----------
    def _backup_dir(self):
        """备份目录：位于 node_library 同级的 node_library_backups，避免被自身打包。"""
        d = os.path.join(hou.homeHoudiniDirectory(), "node_library_backups")
        os.makedirs(d, exist_ok=True)
        return d

    def _backup_index_path(self):
        return os.path.join(self._backup_dir(), "backup_index.json")

    def _load_backup_index(self):
        """读取备份索引（版本列表），失败返回空列表。"""
        try:
            p = self._backup_index_path()
            if os.path.exists(p):
                with open(p, "r", encoding="utf-8") as f:
                    data = json.load(f)
                return data if isinstance(data, list) else []
        except Exception:
            pass
        return []

    def _save_backup_index(self, index):
        try:
            with open(self._backup_index_path(), "w", encoding="utf-8") as f:
                json.dump(index, f, ensure_ascii=False, indent=2)
        except Exception as e:
            hou.ui.displayMessage(f"备份索引写入失败：{e}", severity=hou.severityType.Error)

    def _count_groups(self):
        """统计库中节点组（.cpio）总数。"""
        count = 0
        for root, dirs, files in os.walk(self.root_path):
            count += sum(1 for f in files if f.endswith(".cpio"))
        return count

    def _next_backup_version(self):
        """计算下一个备份版本号（现有最大版本 + 1）。"""
        index = self._load_backup_index()
        if not index:
            return 1
        return max(int(rec.get("version", 0)) for rec in index) + 1

    def _create_backup(self, note="", silent=False):
        """将整个库打包为 zip（排除 .trash），记录版本号到索引，返回 (zip路径, 记录)。"""
        version = self._next_backup_version()
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"backup_v{version:03d}_{ts}.zip"
        zip_path = os.path.join(self._backup_dir(), filename)

        # 打包 root_path 下所有内容，排除 .trash 回收目录以节省空间
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for root, dirs, files in os.walk(self.root_path):
                dirs[:] = [d for d in dirs if d != ".trash"]
                for f in files:
                    full = os.path.join(root, f)
                    rel = os.path.relpath(full, self.root_path).replace("\\", "/")
                    zf.write(full, rel)

        rec = {
            "version": version,
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "file": filename,
            "note": note,
            "groups": self._count_groups(),
        }
        index = self._load_backup_index()
        index.append(rec)
        self._save_backup_index(index)

        if not silent:
            self.status_bar.showMessage(f"已创建备份 v{version}：{filename}", 5000)
        return zip_path, rec

    def backup_library(self):
        """手动备份：输入可选备注后打包整库。"""
        note, ok = QtWidgets.QInputDialog.getText(self, "备份库", "备份备注（可选）：")
        if not ok:
            return
        try:
            zip_path, rec = self._create_backup(note=note.strip())
        except Exception as e:
            hou.ui.displayMessage(f"备份失败：{e}", severity=hou.severityType.Error)
            return
        hou.ui.displayMessage(
            f"已创建备份 v{rec['version']}\n{zip_path}\n共 {rec['groups']} 个节点组。"
        )

    def _clear_library(self):
        """清空库目录（恢复前调用，保留根目录本身）。"""
        for entry in os.listdir(self.root_path):
            p = os.path.join(self.root_path, entry)
            if os.path.isdir(p):
                shutil.rmtree(p)
            else:
                os.remove(p)

    def restore_library(self):
        """从备份恢复：选择版本 → 确认 → 自动备份当前状态 → 清空并解压。"""
        index = self._load_backup_index()
        if not index:
            hou.ui.displayMessage("暂无可用备份。", severity=hou.severityType.Warning)
            return

        # 倒序显示，最新在前
        rev = list(reversed(index))
        labels = [
            f"v{rec['version']}  {rec['timestamp']}  节点组 {rec.get('groups', 0)}  {rec.get('note', '')}"
            for rec in rev
        ]
        choice, ok = QtWidgets.QInputDialog.getItem(
            self, "恢复库", "选择要恢复的备份：", labels, 0, False
        )
        if not ok:
            return
        rec = rev[labels.index(choice)]

        ret = hou.ui.displayMessage(
            f"恢复 v{rec['version']}（{rec['timestamp']}）将覆盖当前库的全部内容。\n"
            "恢复前会自动创建当前状态的备份，是否继续？",
            buttons=("恢复", "取消"), default_choice=1,
        )
        if ret != 0:
            return

        try:
            # 1. 恢复前自动备份当前状态，作为安全网
            self._create_backup(note="恢复前自动备份", silent=True)
            # 2. 清空当前库
            self._clear_library()
            # 3. 解压所选备份
            zip_path = os.path.join(self._backup_dir(), rec["file"])
            if not os.path.exists(zip_path):
                raise RuntimeError(f"备份文件不存在：{zip_path}")
            with zipfile.ZipFile(zip_path, "r") as zf:
                zf.extractall(self.root_path)
        except Exception as e:
            hou.ui.displayMessage(f"恢复失败：{e}", severity=hou.severityType.Error)
            return

        # 恢复后清空撤销栈（旧命令已不适用），并刷新界面
        self.undo_stack.clear()
        self._search_text = ""
        self.search_edit.clear()
        self.refresh_tree()
        self.status_bar.showMessage(f"已恢复 v{rec['version']}", 5000)

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
            screenshot_action = menu.addAction("截图 (Ctrl+Shift+A)")
            new_folder_action = menu.addAction("新建分类")
            menu.addSeparator()
            backup_action = menu.addAction("备份库")
            restore_action = menu.addAction("恢复库")
            menu.addSeparator()
            undo_action = menu.addAction("撤销 (Ctrl+Z)")
            redo_action = menu.addAction("恢复 (Ctrl+Y)")
            action = menu.exec_(self.tree.viewport().mapToGlobal(pos))
            if action == screenshot_action:
                self.capture_screenshot()
            elif action == new_folder_action:
                self.create_folder()
            elif action == backup_action:
                self.backup_library()
            elif action == restore_action:
                self.restore_library()
            elif action == undo_action:
                self.undo()
            elif action == redo_action:
                self.redo()
            return

        data = item.data(0, QtCore.Qt.UserRole)
        menu = QtWidgets.QMenu()

        screenshot_action = menu.addAction("截图 (Ctrl+Shift+A)")
        menu.addSeparator()

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

        if action == screenshot_action:
            self.capture_screenshot()
        elif action == rename_action:
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

    # ---------- 截图 ----------
    def capture_screenshot(self):
        """为当前选中的节点组创建截图：隐藏面板后抓屏并显示选区覆盖层。"""
        data = self._selected_item_data()
        if not data or data["type"] != "group":
            hou.ui.displayMessage("请先在库中选择一个节点组。", severity=hou.severityType.Warning)
            return
        self._screenshot_target = data
        self.hide()
        # 延迟等待面板完全隐藏，避免自身被截入画面
        QtCore.QTimer.singleShot(180, self._start_screenshot)

    def _start_screenshot(self):
        """抓取当前主屏幕并弹出全屏选区覆盖层。"""
        screen = QtWidgets.QApplication.primaryScreen()
        pixmap = screen.grabWindow(0)  # 参数 0 表示抓取整个屏幕
        if pixmap.isNull():
            self.show()
            hou.ui.displayMessage("截图失败：无法获取屏幕画面。", severity=hou.severityType.Error)
            return
        # 保存引用，避免覆盖层被垃圾回收
        self._screenshot_overlay = ScreenCaptureOverlay(
            pixmap,
            on_captured=self._on_screenshot_captured,
            on_cancelled=self._on_screenshot_cancelled,
        )
        self._screenshot_overlay.show()
        self._screenshot_overlay.raise_()
        self._screenshot_overlay.activateWindow()

    def _on_screenshot_cancelled(self):
        """截图被取消：恢复面板并给出状态栏反馈。"""
        self.show()
        self.status_bar.showMessage("截图已取消", 3000)

    def _on_screenshot_captured(self, pixmap):
        """截图完成：保存到组所在文件夹（相对路径）并刷新预览口。"""
        self.show()
        if pixmap is None or pixmap.isNull():
            self.status_bar.showMessage("截图失败", 3000)
            return
        data = getattr(self, "_screenshot_target", None)
        if not data:
            self.status_bar.showMessage("截图目标已丢失", 3000)
            return
        try:
            rel_name = self._save_thumb_for_group(data, pixmap)
        except Exception as e:
            hou.ui.displayMessage(f"截图保存失败：{e}", severity=hou.severityType.Error)
            return
        # 重新加载树，使树节点携带最新 meta（含 thumbnail），保证切换后预览仍能显示
        self.refresh_tree()
        self.status_bar.showMessage(f"截图已保存：{rel_name}", 5000)

    def _save_thumb_for_group(self, data, pixmap):
        """将截图保存到组所在文件夹，并以相对文件名记录到组元数据。

        采用与 .cpio 同名的 .png（即 <file>.png）作为截图文件，
        meta 中的 thumbnail 字段仅保存相对文件名，便于整体移动/复制。
        """
        folder = data["folder"]
        rel_name = data["file"] + ".png"
        abs_path = os.path.join(folder, rel_name)
        if not pixmap.save(abs_path, "PNG"):
            raise RuntimeError(f"无法写入文件：{abs_path}")
        meta = self._load_meta(folder)
        for g in meta["groups"]:
            if g["file"] == data["file"]:
                g["thumbnail"] = rel_name
                break
        self._save_meta(folder, meta)
        return rel_name

    def on_thumb_context_menu(self, pos):
        """截图预览口右键菜单：重新截图 / 删除截图。"""
        data = self._selected_item_data()
        if not data or data["type"] != "group":
            return
        menu = QtWidgets.QMenu(self)
        recapture_action = menu.addAction("重新截图")
        delete_action = menu.addAction("删除截图")
        action = menu.exec_(self.thumb_label.mapToGlobal(pos))
        if action == recapture_action:
            self.capture_screenshot()
        elif action == delete_action:
            self._delete_thumb()

    def _delete_thumb(self):
        """删除当前组的截图文件并清理元数据字段。"""
        data = self._selected_item_data()
        if not data or data["type"] != "group":
            return
        ret = hou.ui.displayMessage(
            "确定删除该节点组的截图吗？", buttons=("删除", "取消"), default_choice=1
        )
        if ret != 0:
            return
        path = self._thumb_path(data)
        if path and os.path.exists(path):
            try:
                os.remove(path)
            except Exception as e:
                hou.ui.displayMessage(f"删除截图文件失败：{e}", severity=hou.severityType.Error)
                return
        meta = self._load_meta(data["folder"])
        for g in meta["groups"]:
            if g["file"] == data["file"]:
                g.pop("thumbnail", None)
                break
        self._save_meta(data["folder"], meta)
        # 重新加载树，刷新树节点内存中的 meta，保证预览口与数据一致
        self.refresh_tree()
        self.status_bar.showMessage("截图已删除", 3000)


# ============================================================
# 截图：全屏选区覆盖层
# ============================================================
class ScreenCaptureOverlay(QtWidgets.QWidget):
    """全屏截图选区覆盖层。

    原理：先在内存中抓取一张全屏图作为背景，再用无边框置顶窗口铺满屏幕，
    在该窗口上绘制半透明遮罩与选区，用户通过拖动/点击完成截图。
    支持三种模式：
      - 自由选择：默认模式，拖动鼠标框选区域；
      - 窗口选择：按 W 键切换，悬停高亮窗口后点击捕获该窗口；
      - 全屏捕获：按 F 键或双击鼠标，捕获整屏。
    """

    def __init__(self, screen_pixmap, on_captured, on_cancelled):
        super().__init__(
            None,
            QtCore.Qt.FramelessWindowHint
            | QtCore.Qt.WindowStaysOnTopHint
            | QtCore.Qt.Tool,
        )
        self._pixmap = screen_pixmap          # 全屏背景图
        self._on_captured = on_captured        # 捕获完成回调
        self._on_cancelled = on_cancelled      # 取消回调

        self._drag_start = None        # 自由选择起点
        self._drag_end = None          # 自由选择终点
        self._window_mode = False      # 是否处于窗口选择模式
        self._highlight_rect = None    # 窗口模式下高亮的窗口矩形
        self._windows = None           # 窗口枚举结果缓存
        self._done = False             # 防止回调被重复触发

        self.setMouseTracking(True)
        self.setCursor(QtCore.Qt.CrossCursor)
        self.setFocusPolicy(QtCore.Qt.StrongFocus)
        # 覆盖当前主屏幕
        screen = QtWidgets.QApplication.primaryScreen()
        self.setGeometry(screen.geometry())

    # ---------- 绘制 ----------
    def paintEvent(self, event):
        painter = QtGui.QPainter(self)
        painter.drawPixmap(0, 0, self._pixmap)
        painter.fillRect(self.rect(), QtGui.QColor(0, 0, 0, 130))

        # 计算当前选区 / 高亮矩形
        sel = None
        if self._window_mode and self._highlight_rect is not None:
            sel = self._highlight_rect
        elif not self._window_mode and self._drag_start is not None and self._drag_end is not None:
            sel = QtCore.QRect(self._drag_start, self._drag_end).normalized()

        if sel is not None and sel.width() > 0 and sel.height() > 0:
            # 将选区还原为原始亮色图像并描边
            painter.drawPixmap(sel, self._pixmap, sel)
            painter.setPen(QtGui.QPen(QtGui.QColor(0, 160, 255), 2))
            painter.drawRect(sel)
            # 显示选区尺寸
            painter.setPen(QtGui.QColor(255, 255, 255))
            painter.drawText(sel.left(), max(sel.top() - 6, 16),
                             f"{sel.width()} × {sel.height()}")

        # 顶部提示条
        painter.fillRect(0, 0, self.width(), 30, QtGui.QColor(0, 0, 0, 160))
        painter.setPen(QtGui.QColor(255, 255, 255))
        mode_txt = "窗口选择" if self._window_mode else "自由选择"
        tip = (f"模式：{mode_txt} | 拖动/点击选择 | 双击=全屏 | Enter=确认 | "
               f"W=窗口 | F=全屏 | Esc=取消 | 右键切换模式")
        painter.drawText(QtCore.QRect(10, 8, self.width() - 20, 20),
                         QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter, tip)

    def _selection_rect(self):
        """根据起点与终点构造规范化矩形。"""
        return QtCore.QRect(self._drag_start, self._drag_end).normalized()

    # ---------- 鼠标交互 ----------
    def mousePressEvent(self, event):
        if event.button() != QtCore.Qt.LeftButton:
            return
        if self._window_mode:
            self._capture_window_at(event.pos())
        else:
            self._drag_start = event.pos()
            self._drag_end = event.pos()
            self.update()

    def mouseMoveEvent(self, event):
        if self._window_mode:
            self._highlight_rect = self._window_rect_at(event.pos())
            self.update()
        elif self._drag_start is not None:
            self._drag_end = event.pos()
            self.update()

    def mouseReleaseEvent(self, event):
        if event.button() != QtCore.Qt.LeftButton or self._window_mode:
            return
        self._drag_end = event.pos()
        rect = self._selection_rect()
        if rect.width() >= 2 and rect.height() >= 2:
            self._finish(rect)
        self._drag_start = None
        self._drag_end = None
        self.update()

    def mouseDoubleClickEvent(self, event):
        # 双击直接全屏捕获
        if event.button() == QtCore.Qt.LeftButton:
            self._finish_fullscreen()

    # ---------- 键盘交互 ----------
    def keyPressEvent(self, event):
        key = event.key()
        if key == QtCore.Qt.Key_Escape:
            self._cancel()
        elif key in (QtCore.Qt.Key_Return, QtCore.Qt.Key_Enter):
            if self._window_mode and self._highlight_rect is not None:
                self._finish(self._highlight_rect)
            else:
                self._finish_fullscreen()
        elif key == QtCore.Qt.Key_W:
            self._set_window_mode(not self._window_mode)
        elif key == QtCore.Qt.Key_F:
            self._finish_fullscreen()
        else:
            super().keyPressEvent(event)

    # ---------- 右键菜单 ----------
    def contextMenuEvent(self, event):
        menu = QtWidgets.QMenu(self)
        free_action = menu.addAction("自由选择")
        win_action = menu.addAction("窗口选择")
        full_action = menu.addAction("全屏截图")
        menu.addSeparator()
        cancel_action = menu.addAction("取消")
        action = menu.exec_(event.globalPos())
        if action == free_action:
            self._set_window_mode(False)
        elif action == win_action:
            self._set_window_mode(True)
        elif action == full_action:
            self._finish_fullscreen()
        elif action == cancel_action:
            self._cancel()

    def _set_window_mode(self, enabled):
        """切换窗口选择模式并重置状态。"""
        self._window_mode = enabled
        self._highlight_rect = None
        self._drag_start = None
        self._drag_end = None
        self.setCursor(
            QtCore.Qt.PointingHandCursor if enabled else QtCore.Qt.CrossCursor
        )
        self.update()

    # ---------- 窗口枚举与捕获 ----------
    def _enumerate_windows(self):
        """枚举当前可见的顶层窗口（Windows 通过 Win32 API，其余平台返回空）。

        通过 EnumWindows 按 Z 序（从上到下）遍历，排除自身、不可见窗口、
        无标题窗口与零尺寸窗口，返回含标题与矩形信息的列表。
        """
        windows = []
        if os.name != "nt":
            return windows
        try:
            import ctypes
            from ctypes import wintypes

            user32 = ctypes.windll.user32
            self_handle = int(self.winId())

            EnumWindowsProc = ctypes.WINFUNCTYPE(
                wintypes.BOOL, wintypes.HWND, wintypes.LPARAM
            )

            def _callback(hwnd, _lparam):
                h = wintypes.HWND(hwnd)
                if int(h) == self_handle or not user32.IsWindowVisible(h):
                    return True
                length = user32.GetWindowTextLengthW(h)
                if length <= 0:
                    return True
                buf = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(h, buf, length + 1)
                if not buf.value:
                    return True
                rect = wintypes.RECT()
                if not user32.GetWindowRect(h, ctypes.byref(rect)):
                    return True
                w = rect.right - rect.left
                hgt = rect.bottom - rect.top
                if w <= 0 or hgt <= 0:
                    return True
                windows.append({
                    "title": buf.value,
                    "rect": QtCore.QRect(rect.left, rect.top, w, hgt),
                })
                return True

            user32.EnumWindows(EnumWindowsProc(_callback), 0)
        except Exception:
            # 非 Windows 或 API 不可用时静默降级为自由选择
            pass
        return windows

    def _window_rect_at(self, pos):
        """返回包含给定坐标且 Z 序最靠前的窗口矩形。"""
        if self._windows is None:
            self._windows = self._enumerate_windows()
        for win in self._windows:
            if win["rect"].contains(pos):
                return win["rect"]
        return None

    def _capture_window_at(self, pos):
        """在窗口模式下点击时，捕获命中的窗口区域。"""
        rect = self._window_rect_at(pos)
        if rect is not None:
            self._finish(rect)

    # ---------- 完成 / 取消 ----------
    def _finish(self, rect):
        if self._done:
            return
        # 与屏幕矩形求交集，防止越界
        rect = rect.intersected(self.rect())
        if rect.width() <= 0 or rect.height() <= 0:
            return
        self._done = True
        pixmap = self._pixmap.copy(rect)
        self.close()
        self._on_captured(pixmap)

    def _finish_fullscreen(self):
        if self._done:
            return
        self._done = True
        pixmap = self._pixmap.copy()
        self.close()
        self._on_captured(pixmap)

    def _cancel(self):
        if self._done:
            return
        self._done = True
        self.close()
        self._on_cancelled()


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
