"""Daily note board and optional desktop widgets."""
from datetime import datetime, timedelta
from uuid import uuid4

from PySide6.QtCore import QDateTime, QMimeData, QPoint, Qt, QTimer
from PySide6.QtGui import QAction, QColor, QDrag
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDateTimeEdit, QDialog, QDialogButtonBox,
    QFormLayout, QFrame, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QMenu, QMessageBox, QPushButton, QTextEdit, QVBoxLayout, QWidget,
)

LANES = {'doing': '现在做', 'waiting': '等别人', 'done': '做完了'}
ROLE = Qt.ItemDataRole.UserRole
NOTE_MIME = 'application/x-niuma-board-note'


def button(text, layout, slot):
    control = QPushButton(text)
    control.clicked.connect(slot)
    layout.addWidget(control)
    return control


class BoardColumn(QListWidget):
    def __init__(self, board, lane):
        super().__init__()
        self.board = board
        self.lane = lane
        self.setAccessibleName(LANES[lane])
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.viewport().setAcceptDrops(True)
        self.setDragDropMode(QListWidget.DragDropMode.DragDrop)
        self.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.itemActivated.connect(lambda item: board.owner.select_id(item.data(ROLE)))
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self.menu)

    def menu(self, point):
        item = self.itemAt(point)
        if not item:
            return
        ident = item.data(ROLE)
        menu = QMenu(self)
        menu.addAction('打开便签', lambda: self.board.owner.select_id(ident))
        menu.addAction('浮动显示', lambda: self.board.owner.open_sticky(ident))
        menu.addSeparator()
        for lane, title in LANES.items():
            if lane != self.lane:
                menu.addAction('移到「' + title + '」', lambda target=lane: self.board.move_note(ident, target))
        menu.exec(self.viewport().mapToGlobal(point))

    def keyPressEvent(self, event):
        if event.modifiers() == Qt.KeyboardModifier.ControlModifier and self.currentItem():
            keys = (Qt.Key.Key_1, Qt.Key.Key_2, Qt.Key.Key_3)
            if event.key() in keys:
                lane = tuple(LANES)[keys.index(event.key())]
                self.board.move_note(self.currentItem().data(ROLE), lane)
                return
        super().keyPressEvent(event)

    def startDrag(self, supported_actions):
        item = self.currentItem()
        if not item:
            return
        mime = QMimeData()
        mime.setData(NOTE_MIME, item.data(ROLE).encode('ascii'))
        drag = QDrag(self)
        drag.setMimeData(mime)
        drag.exec(Qt.DropAction.MoveAction)

    def dragEnterEvent(self, event):
        if event.mimeData().hasFormat(NOTE_MIME):
            event.acceptProposedAction()

    def dragMoveEvent(self, event):
        if event.mimeData().hasFormat(NOTE_MIME):
            event.acceptProposedAction()

    def dropEvent(self, event):
        if event.mimeData().hasFormat(NOTE_MIME):
            ident = bytes(event.mimeData().data(NOTE_MIME)).decode('ascii', errors='ignore')
            if self.board.move_note(ident, self.lane):
                event.acceptProposedAction()


class TodayBoard(QWidget):
    def __init__(self, owner):
        super().__init__(owner)
        self.owner = owner
        self.day = None
        layout = QVBoxLayout(self)
        bar = QHBoxLayout()
        layout.addLayout(bar)
        heading = QLabel('今日便签墙')
        heading.setObjectName('heading')
        bar.addWidget(heading)
        self.date_label = QLabel()
        bar.addWidget(self.date_label)
        bar.addStretch()
        button('截止日贴纸', bar, lambda: owner.desktop.manage())
        columns = QHBoxLayout()
        layout.addLayout(columns, 1)
        self.lists = {}
        self.headings = {}
        for lane, title in LANES.items():
            frame = QFrame()
            frame.setObjectName('noteListPanel')
            column = QVBoxLayout(frame)
            label = QLabel(title)
            label.setStyleSheet('font-size: 16px; font-weight: bold; color: #294632;')
            column.addWidget(label)
            listing = BoardColumn(self, lane)
            column.addWidget(listing, 1)
            button('＋ 添加便签', column, lambda checked=False, target=lane: self.add_note(target))
            columns.addWidget(frame, 1)
            self.lists[lane] = listing
            self.headings[lane] = label

    def refresh(self):
        day = datetime.now().date().isoformat()
        self.day = day
        self.date_label.setText(day)
        selected = {lane: view.currentItem().data(ROLE) if view.currentItem() else None
                    for lane, view in self.lists.items()}
        for listing in self.lists.values():
            listing.clear()
        for note in self.owner.store.all('active'):
            if note['note_date'] != day:
                continue
            lane = note['board_lane'] if note['board_lane'] in LANES else 'doing'
            preview = ' '.join(note['content'].split())[:72]
            detail = note['category'] + (' · ' + preview if preview else '')
            item = QListWidgetItem(('★ ' if note['pinned'] else '') + note['title'] + '\n' + detail)
            item.setData(ROLE, note['id'])
            item.setToolTip(note['title'])
            self.lists[lane].addItem(item)
            if note['id'] == selected[lane]:
                self.lists[lane].setCurrentItem(item)
        for lane, label in self.headings.items():
            label.setText(f'{LANES[lane]} · {self.lists[lane].count()}')

    def move_note(self, ident, lane):
        store = self.owner.store
        note = store.get(ident)
        if (not note or lane not in LANES or note['archived'] or note['trashed'] or
                note['note_date'] != datetime.now().date().isoformat()):
            return False
        if note['board_lane'] != lane:
            store.update(ident, board_lane=lane)
            self.owner.reload_list(keep=True)
            self.refresh()
        view = self.lists[lane]
        for index in range(view.count()):
            if view.item(index).data(ROLE) == ident:
                view.setCurrentRow(index)
                view.setFocus()
                break
        return True

    def add_note(self, lane):
        self.owner.new_note(note_date=datetime.now().date().isoformat(), board_lane=lane)


def countdown(deadline, now):
    distance = (deadline - now).total_seconds()
    if abs(distance) < 60:
        return '即将截止' if distance > 0 else '已到截止时间'
    minutes = int(abs(distance) // 60)
    days, remainder = divmod(minutes, 1440)
    hours, minutes = divmod(remainder, 60)
    parts = [f'{count}{unit}' for count, unit in ((days, '天'), (hours, '小时'), (minutes, '分钟')) if count]
    return ('还剩 ' if distance > 0 else '已逾期 ') + ' '.join(parts[:2])


class FloatingCard(QWidget):
    def __init__(self, owner, title, position_changed):
        flags = Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint
        super().__init__(None, flags)
        self.setWindowTitle(title)
        self.setWindowIcon(owner.icon)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.position_changed = position_changed
        self.drag_offset = None
        self.outer = QVBoxLayout(self)
        self.outer.setContentsMargins(0, 0, 0, 0)
        self.frame = QFrame()
        self.frame.setObjectName('desktopCard')
        self.frame.setStyleSheet("""
            QFrame#desktopCard { background: #f5f9ee; border: 1px solid #a8bd9f; border-radius: 13px; }
            QLabel, QCheckBox { color: #24392c; }
            QPushButton { background: #e4edda; border: none; border-radius: 6px; padding: 4px 8px; color: #263d2e; }
            QPushButton:hover { background: #ccdcbc; }
        """)
        self.outer.addWidget(self.frame)
        self.body = QVBoxLayout(self.frame)
        self.bar = QHBoxLayout()
        self.body.addLayout(self.bar)
        label = QLabel(title)
        label.setStyleSheet('font-weight: bold;')
        label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.bar.addWidget(label)
        self.bar.addStretch()

    def place(self, saved, offset=0):
        self.adjustSize()
        desktop = QApplication.primaryScreen()
        if not desktop:
            return
        area = desktop.availableGeometry()
        target = QPoint(*saved) if saved else QPoint(area.right() - self.width() - 16,
                                                       area.bottom() - self.height() - 16 - offset)
        screen = QApplication.screenAt(target)
        area = screen.availableGeometry() if screen else area
        self.move(max(area.left(), min(target.x(), area.right() - self.width() + 1)),
                  max(area.top(), min(target.y(), area.bottom() - self.height() + 1)))

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.drag_offset = event.globalPosition().toPoint() - self.pos()
            event.accept()

    def mouseMoveEvent(self, event):
        if self.drag_offset is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self.move(event.globalPosition().toPoint() - self.drag_offset)
            event.accept()

    def mouseReleaseEvent(self, event):
        if self.drag_offset is not None:
            self.drag_offset = None
            self.position_changed([self.x(), self.y()])
            event.accept()


class DeadlineCard(FloatingCard):
    def __init__(self, tools, record):
        self.tools = tools
        self.record = record
        super().__init__(tools.owner, '截止日贴纸', self.save_position)
        self.setFixedWidth(276)
        button('编辑', self.bar, lambda: tools.edit(record=self.record))
        close = button('×', self.bar, self.close)
        close.setAccessibleName('隐藏截止日贴纸')
        self.title = QLabel()
        self.title.setTextFormat(Qt.TextFormat.PlainText)
        self.title.setWordWrap(True)
        self.title.setStyleSheet('font-size: 16px; font-weight: bold;')
        self.body.addWidget(self.title)
        self.remaining = QLabel()
        self.remaining.setStyleSheet('font-weight: bold; color: #8c4e2c;')
        self.body.addWidget(self.remaining)
        self.time_label = QLabel()
        self.body.addWidget(self.time_label)
        self.steps = QVBoxLayout()
        self.body.addLayout(self.steps)
        self.render()

    def render(self):
        deadline = datetime.fromisoformat(self.record['deadline'])
        self.title.setText(self.record['title'])
        self.time_label.setText(deadline.strftime('%Y-%m-%d %H:%M'))
        self.tick()
        while self.steps.count():
            child = self.steps.takeAt(0).widget()
            child.hide()
            child.deleteLater()
        for index, step in enumerate(self.record['steps']):
            control = QCheckBox(step['title'].replace('&', '&&'))
            control.setChecked(step['done'])
            control.toggled.connect(lambda checked, slot=index: self.toggle_step(slot, checked))
            self.steps.addWidget(control)
        self.adjustSize()

    def save_position(self, position):
        self.record['position'] = position
        self.tools.save(self.record)

    def toggle_step(self, index, checked):
        self.record['steps'][index]['done'] = checked
        self.tools.save(self.record)

    def tick(self):
        self.remaining.setText(countdown(datetime.fromisoformat(self.record['deadline']), datetime.now()))

    def closeEvent(self, event):
        self.record['visible'] = False
        self.tools.save(self.record)
        event.accept()


class CompanionCard(FloatingCard):
    def __init__(self, tools):
        self.tools = tools
        super().__init__(tools.owner, '牛马搭档', lambda pos: tools.settings.set_setting('companion_position', pos))
        self.setFixedWidth(205)
        button('×', self.bar, self.close).setAccessibleName('关闭牛马搭档')
        self.face = QLabel()
        self.face.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.face.setStyleSheet('font-size: 36px;')
        self.body.addWidget(self.face)
        self.message = QLabel()
        self.message.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.message.setTextFormat(Qt.TextFormat.PlainText)
        self.message.setWordWrap(True)
        self.body.addWidget(self.message)
        choice = QComboBox()
        choice.addItem('小牛', 'cow')
        choice.addItem('小马', 'horse')
        choice.setCurrentIndex(max(0, choice.findData(tools.settings.setting('companion_animal', 'cow'))))
        choice.currentIndexChanged.connect(lambda _: self.change_animal(choice.currentData()))
        self.body.addWidget(choice)
        self.tick(tools.records())

    def change_animal(self, animal):
        self.tools.settings.set_setting('companion_animal', animal)
        self.tick(self.tools.records())

    def tick(self, entries):
        now = datetime.now()
        face = '🐮' if self.tools.settings.setting('companion_animal', 'cow') == 'cow' else '🐴'
        near = [record for record in entries if record['visible'] and
                now <= datetime.fromisoformat(record['deadline']) <= now + timedelta(hours=2)]
        if near:
            closest = min(near, key=lambda record: record['deadline'])
            message = closest['title'][:42] + '\n' + countdown(datetime.fromisoformat(closest['deadline']), now)
        elif now.hour == 12:
            face += ' 💤'
            message = '午休时间'
        else:
            message = '陪你一起做事'
        self.face.setText(face)
        self.message.setText(message)

    def closeEvent(self, event):
        self.tools.companion_action.setChecked(False)
        event.accept()


class DesktopTools:
    def __init__(self, owner):
        self.owner = owner
        self.settings = owner.calendar_page.data
        self.cards = {}
        self.companion = None
        self.companion_action = QAction('显示牛马搭档', owner, checkable=True)
        self.companion_action.setChecked(self.settings.setting('companion_enabled', False))
        self.companion_action.toggled.connect(self.set_companion)
        self.timer = QTimer(owner, interval=15000)
        self.timer.timeout.connect(self.tick)

    def start(self):
        for index, record in enumerate(self.records()):
            if record['visible']:
                self.present(record, 120 + 54 * index)
        self.set_companion(self.companion_action.isChecked())
        self.timer.start()

    def records(self):
        return self.settings.setting('desktop_deadlines', [])

    def save(self, record):
        records = self.records()
        for index, existing in enumerate(records):
            if existing['id'] == record['id']:
                records[index] = record
                break
        else:
            records.append(record)
        self.settings.set_setting('desktop_deadlines', records)

    def delete(self, record):
        widget = self.cards.pop(record['id'], None)
        if widget:
            widget.hide()
            widget.deleteLater()
        self.settings.set_setting('desktop_deadlines', [item for item in self.records() if item['id'] != record['id']])

    def present(self, record, offset=120):
        card = self.cards.get(record['id'])
        if not card:
            card = DeadlineCard(self, record)
            self.cards[record['id']] = card
        card.place(record.get('position'), offset)
        card.show()

    def tick(self):
        if self.owner.stack.currentWidget() is self.owner.board:
            if self.owner.board.day != datetime.now().date().isoformat():
                self.owner.board.refresh()
        for card in self.cards.values():
            if card.isVisible():
                card.tick()
        if self.companion and self.companion.isVisible():
            self.companion.tick(self.records())

    def set_companion(self, enabled):
        if self.settings.setting('companion_enabled', False) != enabled:
            self.settings.set_setting('companion_enabled', enabled)
        if enabled:
            if not self.companion:
                self.companion = CompanionCard(self)
            self.companion.place(self.settings.setting('companion_position', None))
            self.companion.show()
            self.companion.tick(self.records())
        elif self.companion:
            self.companion.hide()

    def shutdown(self):
        self.timer.stop()
        for card in self.cards.values():
            card.hide()
        if self.companion:
            self.companion.hide()

    def edit(self, checked=False, *, record=None, occurrence=None, parent=None):
        if record is None:
            now = datetime.now().replace(second=0, microsecond=0)
            record = dict(id=uuid4().hex, title=occurrence.event['title'][:120] if occurrence else '',
                          deadline=(occurrence.start if occurrence else now + timedelta(days=1)).isoformat(timespec='minutes'),
                          steps=[], visible=True)
        dialog = QDialog(parent or self.owner)
        dialog.setWindowTitle('截止日贴纸')
        dialog.resize(470, 310)
        form = QFormLayout(dialog)
        title = QLineEdit(record['title'])
        title.setMaxLength(120)
        deadline = QDateTimeEdit(QDateTime.fromString(record['deadline'], Qt.DateFormat.ISODate))
        deadline.setCalendarPopup(True)
        deadline.setDisplayFormat('yyyy-MM-dd HH:mm')
        steps = QTextEdit()
        steps.setAcceptRichText(False)
        steps.setPlaceholderText('每行一个步骤，例如检查报价表、上传附件')
        steps.setPlainText('\n'.join(step['title'] for step in record['steps']))
        visible = QCheckBox('显示在桌面')
        visible.setChecked(record['visible'])
        for label, field in [('标题', title), ('截止时间', deadline), ('步骤（最多 8 项）', steps), ('', visible)]:
            form.addRow(label, field)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.rejected.connect(dialog.reject)

        def save_form():
            labels = list(dict.fromkeys(line.strip() for line in steps.toPlainText().splitlines() if line.strip()))
            if not title.text().strip() or len(labels) > 8 or any(len(label) > 32 for label in labels):
                QMessageBox.warning(dialog, '检查贴纸内容', '请输入标题。最多 8 个步骤，每项不超过 32 字。')
                return
            previous = {step['title']: step['done'] for step in record['steps']}
            record.update(title=title.text().strip(), deadline=deadline.dateTime().toPython().isoformat(timespec='minutes'),
                          steps=[dict(title=text, done=previous.get(text, False)) for text in labels],
                          visible=visible.isChecked())
            self.save(record)
            card = self.cards.get(record['id'])
            if card:
                card.record = record
                card.render()
            if record['visible']:
                self.present(record, 120 + max(0, len(self.cards) - 1) * 54)
            elif card:
                card.hide()
            dialog.accept()

        buttons.accepted.connect(save_form)
        if occurrence:
            hint = QLabel('默认使用日程开始时间，可在这里调整。贴纸独立保存。')
            hint.setWordWrap(True)
            form.addRow(hint)
        form.addRow(buttons)
        dialog.exec()

    def manage(self):
        dialog = QDialog(self.owner)
        dialog.setWindowTitle('截止日贴纸')
        dialog.resize(500, 360)
        layout = QVBoxLayout(dialog)
        listing = QListWidget()
        layout.addWidget(listing)
        bar = QHBoxLayout()
        layout.addLayout(bar)

        def refresh():
            listing.clear()
            for entry in sorted(self.records(), key=lambda item: item['deadline']):
                visibility = '显示中' if entry['visible'] else '已隐藏'
                item = QListWidgetItem(f'{entry["title"]}\n{entry["deadline"].replace("T", " ")} · {visibility}')
                item.setData(ROLE, entry['id'])
                listing.addItem(item)

        def selected():
            item = listing.currentItem()
            return next((entry for entry in self.records() if item and entry['id'] == item.data(ROLE)), None)

        def edit_selected():
            if record := selected():
                self.edit(record=record, parent=dialog)
                refresh()

        def add():
            self.edit(parent=dialog)
            refresh()

        def remove():
            if (record := selected()) and QMessageBox.question(
                    dialog, '删除贴纸', '确定删除这张截止日贴纸？') == QMessageBox.StandardButton.Yes:
                self.delete(record)
                refresh()

        button('＋ 新建', bar, add)
        button('编辑／显示', bar, edit_selected)
        button('删除', bar, remove)
        button('关闭', bar, dialog.accept)
        listing.itemActivated.connect(edit_selected)
        refresh()
        dialog.exec()
