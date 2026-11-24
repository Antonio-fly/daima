"""Calendar controls integrated with the existing notes view."""
from datetime import date, datetime, time

from PySide6.QtCore import QDate, QDateTime, QEvent, QMimeData, QSignalBlocker, Qt, QTimer, QUrl
from PySide6.QtGui import QColor, QDrag, QFont
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QColorDialog, QComboBox, QDateEdit, QDateTimeEdit, QDialog,
    QDialogButtonBox, QFontComboBox, QFormLayout, QGridLayout, QHBoxLayout, QHeaderView,
    QInputDialog, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMessageBox, QPushButton,
    QSpinBox, QStackedWidget, QTableView, QTableWidget, QTableWidgetItem, QTextEdit,
    QVBoxLayout, QWidget,
)

from calendar_store import DAY, MINUTE, RULES, CalendarStore, bounds, expand, lunar_labels, official_day


ROLE = Qt.ItemDataRole.UserRole
DRAG_TYPE = 'application/x-niuma-calendar-occurrence'


def button(label, layout, action):
    control = QPushButton(label)
    control.clicked.connect(action)
    layout.addWidget(control)
    return control


def dated(value, parent=None):
    field = QDateEdit(QDate(value), parent)
    field.setDisplayFormat('yyyy-MM-dd')
    field.setCalendarPopup(True)
    return field


def timestamp(value):
    return value.isoformat(timespec='minutes')


def choice(values):
    field = QComboBox()
    for key, title in values:
        field.addItem(title, key)
    return field


def choose_color(control, initial):
    control.value = initial

    def select():
        color = QColorDialog.getColor(QColor(control.value or '#38714e'), control)
        if color.isValid():
            control.value = color.name()
            control.setText(color.name())

    control.clicked.connect(select)
    control.setText(initial or '沿用日历颜色')


class EventEditor(QDialog):
    def __init__(self, page, record, is_new):
        super().__init__(page)
        self.setWindowTitle('新建日程' if is_new else '编辑日程')
        self.record = record
        self.page = page
        form = QFormLayout(self)
        self.resize(525, 650)
        self.title = QLineEdit(record['title'])
        self.start = QDateTimeEdit(QDateTime.fromString(record['start'], Qt.DateFormat.ISODate))
        self.end = QDateTimeEdit(QDateTime.fromString(record['end'], Qt.DateFormat.ISODate))
        for field in (self.start, self.end):
            field.setCalendarPopup(True)
            field.setDisplayFormat('yyyy-MM-dd HH:mm')
        self.all_day = QCheckBox('全天')
        self.all_day.setChecked(record['all_day'])
        self.location = QComboBox()
        self.location.setEditable(True)
        self.location.addItems(page.data.setting('locations', []))
        self.location.setEditText(record['location'])
        self.people = QLineEdit(record['people'])
        self.details = QTextEdit(record['details'])
        self.details.setMaximumHeight(75)
        self.calendar = choice((ident, row['name']) for ident, row in page.data.calendars().items())
        self.calendar.setCurrentIndex(self.calendar.findData(record['calendar']))
        self.color = QPushButton()
        choose_color(self.color, record['color'])
        self.notify = QSpinBox()
        self.notify.setRange(-1, 30 * 1440)
        self.notify.setSpecialValueText('关闭')
        self.notify.setSuffix(' 分钟')
        self.notify.setValue(record['notify'])
        self.repeat_notify = QSpinBox()
        self.repeat_notify.setRange(0, 1440)
        self.repeat_notify.setSpecialValueText('关闭')
        self.repeat_notify.setSuffix(' 分钟')
        self.repeat_notify.setValue(record['repeat_notify'])
        self.rule = choice(RULES.items())
        self.rule.setCurrentIndex(self.rule.findData(record['rule']))
        self.end_mode = choice([('never', '永不结束'), ('until', '截止日期'), ('count', '重复次数')])
        self.end_mode.setCurrentIndex(1 if record['repeat_until'] else 2 if record['repeat_count'] else 0)
        self.until = dated(date.fromisoformat(record['repeat_until']) if record['repeat_until']
                           else self.start.date().toPython() + 30 * DAY)
        self.count = QSpinBox()
        self.count.setRange(1, 999)
        self.count.setValue(record['repeat_count'] or 10)
        for caption, field in [
            ('标题', self.title), ('日历', self.calendar), ('颜色标签', self.color),
            ('开始', self.start), ('结束', self.end), ('', self.all_day),
            ('地点', self.location), ('参与人', self.people), ('备注', self.details),
            ('提前提醒', self.notify), ('重复提醒间隔', self.repeat_notify),
            ('重复规则', self.rule), ('结束条件', self.end_mode),
            ('截止日期', self.until), ('出现次数（含首次）', self.count),
        ]:
            form.addRow(caption, field)
        actions = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        actions.accepted.connect(self.validate)
        actions.rejected.connect(self.reject)
        form.addRow(actions)
        self.rule.currentIndexChanged.connect(self.sync)
        self.end_mode.currentIndexChanged.connect(self.sync)
        self.all_day.toggled.connect(self.sync)
        self.notify.valueChanged.connect(self.sync)
        self.sync()

    def sync(self):
        active = self.rule.currentData() != 'none'
        self.end_mode.setEnabled(active)
        self.until.setEnabled(active and self.end_mode.currentData() == 'until')
        self.count.setEnabled(active and self.end_mode.currentData() == 'count')
        self.repeat_notify.setEnabled(self.notify.value() >= 0)
        self.start.setDisplayFormat('yyyy-MM-dd' if self.all_day.isChecked() else 'yyyy-MM-dd HH:mm')
        self.end.setDisplayFormat(self.start.displayFormat())
        self.end.setToolTip('全天日程的结束日期包含当天' if self.all_day.isChecked() else '')

    def validate(self):
        start, end = self.start.dateTime().toPython(), self.end.dateTime().toPython()
        if self.all_day.isChecked():
            start = datetime.combine(start.date(), time.min)
            end = datetime.combine(end.date() + DAY, time.min)
        title = self.title.text().strip()
        rule = self.rule.currentData()
        until = self.until.date().toPython() if rule != 'none' and self.end_mode.currentData() == 'until' else None
        if not title or end <= start or (until and until < start.date()):
            QMessageBox.warning(self, '检查日程', '请填写标题，确认结束时间晚于开始时间，重复截止日期不早于开始日期。')
            return
        self.result_record = dict(
            self.record, title=title, start=timestamp(start), end=timestamp(end),
            all_day=self.all_day.isChecked(), location=self.location.currentText().strip(),
            people=self.people.text().strip(), details=self.details.toPlainText(), calendar=self.calendar.currentData(),
            color=self.color.value, notify=self.notify.value(),
            repeat_notify=self.repeat_notify.value() if self.notify.value() >= 0 else 0,
            rule=rule, repeat_until=until.isoformat() if until else '',
            repeat_count=self.count.value() if rule != 'none' and self.end_mode.currentData() == 'count' else 0,
        )
        self.accept()


class Timeline(QTableWidget):
    def __init__(self, page):
        super().__init__(49, 1, page)
        self.page = page
        self.drag_index = None
        self.press_point = None
        self.hold = QTimer(self, interval=550, singleShot=True)
        self.hold.timeout.connect(self.quick_create)
        self.setVerticalHeaderLabels(['全天'] + [f'{index // 2:02}:{index % 2 * 30:02}' for index in range(48)])
        self.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.horizontalHeader().setSectionsClickable(True)
        self.horizontalHeader().sectionClicked.connect(lambda column: self.page.jump(self.page.day_columns[column]))
        self.setAcceptDrops(True)
        self.viewport().setAcceptDrops(True)
        self.cellDoubleClicked.connect(self.open_cell)

    def open_cell(self, row, column):
        events = self.page.timeline_entries.get((row, column), [])
        if not events:
            self.page.new_timed(row, column)
        elif len(events) == 1:
            self.page.event_actions(events[0])
        else:
            names = [f'{i + 1}. {item.event["title"]}' for i, item in enumerate(events)]
            name, ok = QInputDialog.getItem(self, '选择日程', '日程', names, editable=False)
            if ok:
                self.page.event_actions(events[names.index(name)])

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.press_point = event.position().toPoint()
            index = self.indexAt(self.press_point)
            self.drag_index = (index.row(), index.column()) if index.isValid() else None
            if self.drag_index and not self.page.timeline_entries.get(self.drag_index):
                self.hold.start()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self.press_point is not None and (event.position().toPoint() - self.press_point).manhattanLength() >= QApplication.startDragDistance():
            self.hold.stop()
            events = self.page.timeline_entries.get(self.drag_index, [])
            if len(events) == 1 and not events[0].event.get('subscription'):
                self.page.dragged = events[0]
                drag = QDrag(self)
                mime = QMimeData()
                mime.setData(DRAG_TYPE, b'event')
                drag.setMimeData(mime)
                self.press_point = None
                drag.exec(Qt.DropAction.MoveAction)
                self.page.dragged = None
                return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        self.hold.stop()
        self.press_point = None
        super().mouseReleaseEvent(event)

    def quick_create(self):
        if self.drag_index:
            self.page.new_timed(*self.drag_index)

    def dragEnterEvent(self, event):
        if event.mimeData().hasFormat(DRAG_TYPE):
            event.acceptProposedAction()

    def dragMoveEvent(self, event):
        if event.mimeData().hasFormat(DRAG_TYPE):
            event.acceptProposedAction()

    def dropEvent(self, event):
        index = self.indexAt(event.position().toPoint())
        if index.isValid() and self.page.dragged and event.mimeData().hasFormat(DRAG_TYPE):
            event.acceptProposedAction()
            self.page.move_event(self.page.dragged, index.row(), index.column())


class CalendarPage(QWidget):
    def __init__(self, owner, month):
        super().__init__(owner)
        self.owner = owner
        self.month = month
        self.data = CalendarStore(owner.store.conn)
        self.dragged = None
        self.range_start = None
        self.timeline_entries = {}
        self.day_columns = []
        self.network = QNetworkAccessManager(self)
        self.feed_refresh = QTimer(self, interval=6 * 60 * 60 * 1000)
        self.feed_refresh.timeout.connect(self.fetch_subscription)
        self.feed_refresh.start()
        self.subscription = None
        self.load_subscription()
        self.month_view = month.findChild(QTableView)
        self.month_viewport = self.month_view.viewport()
        self.month_viewport.installEventFilter(self)
        self.month_hold = QTimer(self, interval=550, singleShot=True)
        self.month_hold.timeout.connect(self.month_long_press)
        self.month_press = None
        self.range_drag = False

        layout = QVBoxLayout(self)
        top = QHBoxLayout()
        layout.addLayout(top)
        self.mode = choice([(0, '月'), (1, '周'), (2, '日'), (3, '年'), (4, '议程')])
        self.mode.setCurrentIndex(self.data.setting('view', 0))
        top.addWidget(self.mode)
        button('‹', top, lambda: self.navigate(-1)).setAccessibleName('上一页')
        button('今天', top, lambda: self.jump(date.today()))
        button('›', top, lambda: self.navigate(1)).setAccessibleName('下一页')
        self.jump_field = dated(month.selectedDate().toPython())
        self.jump_field.setAccessibleName('跳转日期')
        self.jump_field.dateChanged.connect(lambda day: self.jump(day.toPython()))
        top.addWidget(self.jump_field)
        top.addStretch()
        button('日历设置', top, self.settings)
        actions = QHBoxLayout()
        layout.addLayout(actions)
        button('＋ 日程', actions, self.new_event)
        button('＋ 待办', actions, self.edit_task)
        button('待办清单', actions, self.tasks_dialog)
        button('提醒中心', actions, self.notifications)
        actions.addStretch()
        self.range_button = button('框选日期', actions, self.toggle_range)
        self.range_button.setCheckable(True)
        self.range_button.setToolTip('开启后在月历上从起始日期拖到结束日期')
        self.batch_button = button('批量添加便签', actions, self.batch_notes)
        self.batch_button.setVisible(False)
        self.views = QStackedWidget()
        layout.addWidget(self.views, 3)
        self.views.addWidget(month)
        self.timeline = Timeline(self)
        self.views.addWidget(self.timeline)
        self.year = QWidget()
        self.year_layout = QGridLayout(self.year)
        self.views.addWidget(self.year)
        self.agenda = QListWidget()
        self.agenda.setAccessibleName('未来 30 天议程')
        self.agenda.itemActivated.connect(lambda item: self.event_actions(item.data(ROLE)))
        self.views.addWidget(self.agenda)
        self.detail = QLabel()
        self.detail.setWordWrap(True)
        self.detail.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.detail)
        self.today_items = QListWidget()
        self.today_items.setAccessibleName('当天日程与待办')
        self.today_items.itemActivated.connect(self.open_daily_item)
        layout.addWidget(self.today_items, 1)
        self.notes_layout = QVBoxLayout()
        layout.addLayout(self.notes_layout, 1)
        self.mode.currentIndexChanged.connect(self.change_view)
        self.month.selectionChanged.connect(self.refresh)
        self.month.currentPageChanged.connect(lambda *_: self.refresh())
        self.apply_appearance()

    @property
    def selected(self):
        return self.month.selectedDate().toPython()

    def jump(self, day):
        self.month.setSelectedDate(QDate(day))
        self.refresh()

    def navigate(self, amount):
        current = self.month.selectedDate()
        if self.mode.currentData() == 0:
            target = current.addMonths(amount)
        elif self.mode.currentData() == 3:
            target = current.addYears(amount)
        else:
            target = current.addDays(amount * {1: 7, 2: 1, 4: 30}[self.mode.currentData()])
        if target.isValid():
            self.jump(target.toPython())

    def change_view(self):
        self.data.set_setting('view', self.mode.currentData())
        self.refresh()
        if self.mode.currentData() in (1, 2):
            self.timeline.scrollToItem(self.timeline.item(17, 0), QTableWidget.ScrollHint.PositionAtTop)

    def month_day(self, point):
        index = self.month_view.indexAt(point)
        if not index.isValid() or index.row() == 0:
            return None
        model = index.model()
        first = next((row * 7 + col for row in (1, 2) for col in range(7)
                      if str(model.index(row, col).data()) == '1'), None)
        if first is None:
            return None
        return date(self.month.yearShown(), self.month.monthShown(), 1) + (
            index.row() * 7 + index.column() - first) * DAY

    def eventFilter(self, watched, event):
        if watched is self.month_viewport:
            kind = event.type()
            if kind == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.LeftButton:
                day = self.month_day(event.position().toPoint())
                self.month_press = day
                if day and self.range_button.isChecked():
                    self.range_start = day
                    self.range_drag = True
                    self.update_range(day)
                    return True
                if day:
                    self.month_hold.start()
            elif kind == QEvent.Type.MouseButtonDblClick and self.range_button.isChecked():
                return True
            elif kind == QEvent.Type.MouseMove and self.month_press is not None:
                day = self.month_day(event.position().toPoint())
                if day != self.month_press:
                    self.month_hold.stop()
                if self.range_drag:
                    if day:
                        self.update_range(day)
                    return True
            elif kind == QEvent.Type.MouseButtonRelease:
                self.month_hold.stop()
                self.month_press = None
                if self.range_drag:
                    self.range_drag = False
                    return True
        return super().eventFilter(watched, event)

    def month_long_press(self):
        if self.month_press:
            day, self.month_press = self.month_press, None
            self.new_event(day=day)

    def toggle_range(self, checked):
        if checked:
            self.mode.setCurrentIndex(self.mode.findData(0))
            self.range_start = self.selected
            self.update_range(self.selected)
        else:
            self.month.batch_range = None
            self.month.updateCells()
        self.batch_button.setVisible(checked)

    def update_range(self, day):
        self.month.batch_range = tuple(sorted((self.range_start, day)))
        first, last = self.month.batch_range
        self.batch_button.setText(f'批量添加便签 · {(last - first).days + 1} 天')
        self.month.updateCells()

    def batch_notes(self):
        first, last = self.month.batch_range or (self.selected, self.selected)
        dialog = QDialog(self)
        dialog.setWindowTitle('批量添加便签')
        form = QFormLayout(dialog)
        start, end = dated(first), dated(last)
        title = QLineEdit()
        body = QTextEdit()
        form.addRow('开始日期', start)
        form.addRow('结束日期（含当天）', end)
        form.addRow('统一标题', title)
        form.addRow('统一内容', body)
        box = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        box.accepted.connect(dialog.accept)
        box.rejected.connect(dialog.reject)
        form.addRow(box)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        first, last = start.date().toPython(), end.date().toPython()
        count = (last - first).days + 1
        if not 1 <= count <= 366 or not title.text().strip():
            QMessageBox.warning(self, '检查日期范围', '请填写标题，并选择 1 至 366 天。')
            return
        self.owner.store.add_batch(title.text().strip(), body.toHtml(), body.toPlainText(),
                                   [first + index * DAY for index in range(count)])
        self.owner.reload_list(keep=True)
        self.range_button.setChecked(False)
        self.toggle_range(False)
        self.owner.refresh_calendar()

    def refresh(self):
        day = self.selected
        mode = self.mode.currentData()
        self.views.setCurrentIndex({0: 0, 1: 1, 2: 1, 3: 2, 4: 3}[mode])
        with QSignalBlocker(self.jump_field):
            self.jump_field.setDate(QDate(day))
        lunar, term, festivals = lunar_labels(day)
        official = official_day(day) if self.data.setting('official', True) else ''
        self.detail.setText(' · '.join(filter(None, [day.isoformat(), '农历 ' + lunar, term, festivals, official])))
        if mode == 0:
            first = date(self.month.yearShown(), self.month.monthShown(), 1) - 7 * DAY
            last = first + 49 * DAY
            entries = self.items(bounds(first)[0], bounds(last)[0])
            days = [first + index * DAY for index in range(49)]
            self.month.event_days = {day.isoformat() for day in days
                                     if any(item.start < bounds(day)[1] and item.end > bounds(day)[0]
                                            for item in entries)}
        self.month.show_official = self.data.setting('official', True)
        self.month.updateCells()
        self.refresh_day(day)
        if mode in (1, 2):
            self.refresh_timeline(day, mode)
        elif mode == 3:
            self.refresh_year(day)
        elif mode == 4:
            self.agenda.clear()
            start = bounds(day)[0]
            for item in self.items(start, start + 30 * DAY):
                self.add_event_item(self.agenda, item, dated_label=True)

    def items(self, start, end):
        local = self.data.occurrences(start, end)
        if self.subscription is not None:
            from recurring_ical_events import of
            from calendar_store import Occurrence

            def local_time(value):
                if isinstance(value, datetime):
                    return value.astimezone().replace(tzinfo=None) if value.tzinfo else value
                return datetime.combine(value, time.min)

            # A one-day margin accounts for source-zone dates crossing local midnight.
            for item in of(self.subscription, skip_bad_series=True).between(start.date() - DAY, end.date() + DAY):
                if 'DTSTART' not in item:
                    continue
                original = item.decoded('DTSTART')
                begin = local_time(original)
                finish = local_time(item.decoded('DTEND')) if 'DTEND' in item else begin
                all_day = not isinstance(original, datetime)
                if finish <= begin:
                    finish = begin + (DAY if all_day else MINUTE)
                if begin < end and finish > start:
                    event = dict(id=str(item.get('UID', '')), title=str(item.get('SUMMARY', '订阅日程')),
                                 location=str(item.get('LOCATION', '')), details=str(item.get('DESCRIPTION', '')),
                                 people='', all_day=all_day, subscription=True)
                    local.append(Occurrence(event, begin, finish))
        return sorted(local, key=lambda item: (item.start, item.event['title']))

    def event_color(self, item):
        event = item.event
        return '#785196' if event.get('subscription') else event['color'] or self.colors[event['calendar']]['color']

    def add_event_item(self, widget, item, dated_label=False):
        if item.event['all_day']:
            label = '全天'
        else:
            label = item.start.strftime('%H:%M') + '–' + item.end.strftime('%H:%M')
        prefix = item.start.strftime('%m-%d ') if dated_label else ''
        row = QListWidgetItem(prefix + label + '  ' + item.event['title'])
        row.setForeground(QColor(self.event_color(item)))
        row.setData(ROLE, item)
        widget.addItem(row)

    def refresh_day(self, day):
        self.colors = self.data.calendars()
        self.today_items.clear()
        for item in self.items(*bounds(day)):
            self.add_event_item(self.today_items, item)
        for task in self.data.records('tasks'):
            if task['due'] == day.isoformat():
                row = QListWidgetItem(('✓ ' if task['done'] else '□ ') + task['priority'] + ' · ' + task['title'])
                row.setData(ROLE, task)
                self.today_items.addItem(row)

    def refresh_timeline(self, day, mode):
        first = day - day.weekday() * DAY if mode == 1 else day
        self.day_columns = [first + index * DAY for index in range(7 if mode == 1 else 1)]
        self.timeline_entries.clear()
        self.timeline.clearContents()
        self.timeline.setColumnCount(len(self.day_columns))
        self.timeline.setHorizontalHeaderLabels([
            f'{day:%m-%d} 周{"一二三四五六日"[day.weekday()]}' + (' · 便签' if
                self.month.note_counts.get(day.isoformat()) else '') for day in self.day_columns])
        for column, day in enumerate(self.day_columns):
            start, end = bounds(day)
            for item in self.items(start, end):
                local_start, local_end = max(start, item.start), min(end, item.end)
                if item.event['all_day']:
                    rows = [0]
                else:
                    first_slot = int((local_start - start).total_seconds() // 1800)
                    last_slot = int(((local_end - start).total_seconds() - 1) // 1800)
                    rows = range(first_slot + 1, last_slot + 2)
                for row in rows:
                    self.timeline_entries.setdefault((row, column), []).append(item)
        for row in range(self.timeline.rowCount()):
            for column in range(len(self.day_columns)):
                entries = self.timeline_entries.get((row, column), [])
                label = ' / '.join(item.event['title'] for item in entries)
                cell = QTableWidgetItem(label)
                if entries:
                    cell.setForeground(QColor(self.event_color(entries[0])))
                    cell.setToolTip('\n'.join(item.event['title'] for item in entries))
                if any(left.end > right.start and right.end > left.start
                       for index, left in enumerate(entries) for right in entries[index + 1:]):
                    cell.setBackground(QColor('#ffe4ac'))
                elif self.month.note_counts.get(self.day_columns[column].isoformat()):
                    cell.setBackground(QColor('#e5f2dc'))
                self.timeline.setItem(row, column, cell)

    def refresh_year(self, day):
        while self.year_layout.count():
            self.year_layout.takeAt(0).widget().deleteLater()
        for index in range(12):
            first = date(day.year, index + 1, 1)
            following = QDate(first).addMonths(1).toPython()
            count = sum(n for k, n in self.month.note_counts.items() if first.isoformat() <= k < following.isoformat())
            action = QPushButton(f'{index + 1} 月\n{count} 条便签')
            action.clicked.connect(lambda checked, target=first: self.open_month(target))
            self.year_layout.addWidget(action, index // 3, index % 3)

    def open_month(self, day):
        self.mode.setCurrentIndex(self.mode.findData(0))
        self.jump(day)

    def open_daily_item(self, row):
        item = row.data(ROLE)
        if isinstance(item, dict):
            self.edit_task(task=item)
        else:
            self.event_actions(item)

    def new_timed(self, row, column):
        self.new_event(day=self.day_columns[column], slot=row)

    def new_event(self, checked=False, *, day=None, slot=None, task=None):
        day = day or self.selected
        start = datetime.combine(day, time(9)) if slot is None else bounds(day)[0] + max(0, slot - 1) * 30 * MINUTE
        record = dict(title=task['title'] if task else '', start=timestamp(start),
                      end=timestamp(start + (DAY if slot == 0 else 60 * MINUTE)),
                      all_day=slot == 0, location='', people='', details='', calendar=self.data.setting('default', 1),
                      color='', notify=15, repeat_notify=0, rule='none', repeat_until='', repeat_count=0)
        templates = self.data.records('templates')
        if templates and slot is None and not task:
            labels = ['空白日程'] + [template['title'] for template in templates]
            pick, ok = QInputDialog.getItem(self, '日程模板', '模板', labels, editable=False)
            if not ok:
                return
            if pick != labels[0]:
                template = templates[labels.index(pick) - 1]
                length = datetime.fromisoformat(template['end']) - datetime.fromisoformat(template['start'])
                start = datetime.combine(day, datetime.fromisoformat(template['start']).time())
                record = dict(template, start=timestamp(start), end=timestamp(start + length), repeat_until='')
                record.pop('id', None)
        self.edit_event(record, task=task)

    def edit_event(self, record, occurrence=None, task=None):
        view = dict(record)
        if view['all_day']:
            view['end'] = timestamp(datetime.fromisoformat(view['end']) - DAY)
        while True:
            editor = EventEditor(self, view, 'id' not in record)
            if editor.exec() != QDialog.DialogCode.Accepted:
                return
            result = editor.result_record
            if occurrence:
                result['rule'] = 'none'
            decision, target = self.check_conflicts(result, occurrence)
            if decision == 'adjust':
                view = dict(result)
                if view['all_day']:
                    view['end'] = timestamp(datetime.fromisoformat(view['end']) - DAY)
                continue
            if decision == 'cancel':
                return
            if occurrence:
                self.data.override(occurrence, result)
            else:
                result = self.save_event(result, target)
            location = result['location']
            if location:
                locations = self.data.setting('locations', [])
                self.data.set_setting('locations', [location] + [x for x in locations if x != location][:19])
            if task:
                self.data.write('tasks', dict(task, event_id=result['id']))
            self.jump(datetime.fromisoformat(result['start']).date())
            return

    def check_conflicts(self, candidate, occurrence=None):
        start = datetime.fromisoformat(candidate['start'])
        end = datetime.fromisoformat(candidate['end'])
        if candidate['rule'] != 'none':
            last = date.fromisoformat(candidate['repeat_until']) if candidate['repeat_until'] else start.date() + 366 * DAY
            horizon = min(last + DAY, start.date() + 367 * DAY)
            window_end = max(end, datetime.combine(horizon, time.min))
        else:
            window_end = end
        existing = self.data.occurrences(start, window_end, visible_only=False)
        if self.subscription:
            existing += [item for item in self.items(start, window_end) if item.event.get('subscription')]
            existing.sort(key=lambda item: item.start)
        conflicts = {}
        for proposed in expand(candidate, start, window_end):
            for other in existing:
                if other.start >= proposed.end:
                    break
                if other.end <= proposed.start:
                    continue
                if occurrence and not other.event.get('subscription') and self.data.reminder_key(other) == self.data.reminder_key(occurrence):
                    continue
                if not occurrence and other.event['id'] == candidate.get('id'):
                    continue
                conflicts[other.key] = other
        if not conflicts:
            return 'save', None
        box = QMessageBox(self)
        box.setWindowTitle('日程冲突')
        box.setTextFormat(Qt.TextFormat.PlainText)
        label = '\n'.join(f'{item.start:%m-%d %H:%M} · {item.event["title"]}' for item in list(conflicts.values())[:8])
        scope = '（检查首年内的重复实例）' if candidate['rule'] != 'none' else ''
        box.setText('以下日程时间重叠' + scope + '\n' + label)
        adjust = box.addButton('返回调整', QMessageBox.ButtonRole.RejectRole)
        keep = box.addButton('保留重叠', QMessageBox.ButtonRole.AcceptRole)
        merge = None
        other = next(iter(conflicts.values()))
        if (len(conflicts) == 1 and not occurrence and not other.event.get('subscription')
                and not other.event.get('_original_key')
                and candidate['rule'] == other.event['rule'] == 'none'
                and candidate['all_day'] == other.event['all_day']):
            merge = box.addButton('合并日程', QMessageBox.ButtonRole.ActionRole)
        box.setDefaultButton(adjust)
        box.exec()
        clicked = box.clickedButton()
        if merge and clicked == merge:
            return 'save', other
        if clicked == keep:
            return 'save', None
        return 'adjust' if clicked == adjust else 'cancel', None

    def save_event(self, record, merge_with=None):
        if not merge_with:
            return self.data.write('events', record)
        prior = merge_with.event
        result = dict(record, id=record.get('id', prior['id']), title=prior['title'] + ' / ' + record['title'],
                      start=min(record['start'], timestamp(merge_with.start)),
                      end=max(record['end'], timestamp(merge_with.end)))
        for field in ('location', 'people', 'details'):
            result[field] = '\n'.join(dict.fromkeys(filter(None, [prior[field], record[field]])))
        return self.data.merge_events(result, prior['id'])

    def event_actions(self, item):
        event = item.event
        box = QMessageBox(self)
        box.setWindowTitle('日程详情')
        box.setTextFormat(Qt.TextFormat.PlainText)
        box.setText(event['title'])
        box.setInformativeText('\n'.join(filter(None, [f'{item.start:%Y-%m-%d %H:%M} — {item.end:%Y-%m-%d %H:%M}',
                                                     event['location'], event['people'], event['details']])))
        box.addButton('关闭', QMessageBox.ButtonRole.RejectRole)
        if event.get('subscription'):
            box.exec()
            return
        edit = box.addButton('编辑', QMessageBox.ButtonRole.ActionRole)
        remove = box.addButton('删除', QMessageBox.ButtonRole.DestructiveRole)
        task = box.addButton('创建关联待办', QMessageBox.ButtonRole.ActionRole)
        template = box.addButton('保存为模板', QMessageBox.ButtonRole.ActionRole)
        deadline = box.addButton('生成倒计时贴纸', QMessageBox.ButtonRole.ActionRole)
        box.exec()
        action = box.clickedButton()
        if action == deadline:
            self.owner.desktop.edit(occurrence=item)
        elif action == task:
            self.edit_task(task=dict(title=event['title'], due=item.start.date().isoformat(),
                                     priority='普通', done=False, event_id=event['id']))
        elif action == template:
            title, ok = QInputDialog.getText(self, '保存模板', '模板名称', text=event['title'])
            if ok and title.strip():
                record = dict(event, title=title.strip())
                record.pop('id', None)
                record.pop('_original_key', None)
                self.data.write('templates', record)
        elif action in (edit, remove):
            recurring = event['rule'] != 'none' or '_original_key' in event
            scope = '整个系列'
            if recurring:
                scope, ok = QInputDialog.getItem(self, '日程范围', '作用范围', ['仅此日程', '整个系列'], editable=False)
                if not ok:
                    return
            if action == remove:
                if QMessageBox.question(self, '删除日程', '确定删除所选日程？') != QMessageBox.StandardButton.Yes:
                    return
                if recurring and scope == '仅此日程':
                    self.data.override(item)
                else:
                    self.data.remove('events', event['id'])
                self.refresh()
            elif recurring and scope == '仅此日程':
                self.edit_event(dict(event, start=timestamp(item.start), end=timestamp(item.end),
                                     rule='none', repeat_until='', repeat_count=0), occurrence=item)
            else:
                series = next(r for r in self.data.records('events') if r['id'] == event['id'])
                self.edit_event(series)

    def move_event(self, item, row, column):
        start = bounds(self.day_columns[column])[0] + max(0, row - 1) * 30 * MINUTE
        if (row == 0) != item.event['all_day'] or start == item.start:
            return
        record = dict(item.event, start=timestamp(start), end=timestamp(start + (item.end - item.start)))
        recurring = record['rule'] != 'none' or '_original_key' in record
        if recurring:
            record['rule'] = 'none'
        decision, target = self.check_conflicts(record, item if recurring else None)
        if decision == 'adjust':
            self.edit_event(record, occurrence=item if recurring else None)
        elif decision == 'save':
            if recurring:
                self.data.override(item, record)
            else:
                self.save_event(record, target)
            self.refresh()

    def edit_task(self, checked=False, *, task=None):
        task = task or dict(title='', due=self.selected.isoformat(), priority='普通', done=False, event_id='')
        dialog = QDialog(self)
        dialog.setWindowTitle('编辑待办' if task.get('id') else '新建待办')
        form = QFormLayout(dialog)
        title = QLineEdit(task['title'])
        due = dated(date.fromisoformat(task['due']))
        priority = choice((value, value) for value in ('低', '普通', '高'))
        priority.setCurrentIndex(priority.findData(task['priority']))
        done = QCheckBox('已完成')
        done.setChecked(task['done'])
        event = choice([('', '无关联')] + [(entry['id'], entry['title']) for entry in self.data.records('events')])
        event.setCurrentIndex(max(0, event.findData(task['event_id'])))
        for label, field in [('待办', title), ('截止日期', due), ('优先级', priority), ('状态', done), ('关联日程', event)]:
            form.addRow(label, field)
        box = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        saved = box.button(QDialogButtonBox.StandardButton.Save)
        saved.setEnabled(bool(title.text().strip()))
        title.textChanged.connect(lambda value: saved.setEnabled(bool(value.strip())))
        box.accepted.connect(dialog.accept)
        box.rejected.connect(dialog.reject)
        form.addRow(box)
        linked = box.addButton('查看关联日程', QDialogButtonBox.ButtonRole.ActionRole)
        linked.setEnabled(bool(event.currentData()))
        event.currentIndexChanged.connect(lambda: linked.setEnabled(bool(event.currentData())))

        def open_linked():
            from calendar_store import Occurrence

            record = next((entry for entry in self.data.records('events') if entry['id'] == event.currentData()), None)
            if record:
                self.event_actions(Occurrence(record, datetime.fromisoformat(record['start']),
                                              datetime.fromisoformat(record['end'])))

        linked.clicked.connect(open_linked)
        if task.get('id'):
            plan = box.addButton('安排成日程', QDialogButtonBox.ButtonRole.ActionRole)
            plan.setEnabled(not bool(event.currentData()))
            event.currentIndexChanged.connect(lambda: plan.setEnabled(not bool(event.currentData())))
            plan.clicked.connect(lambda: dialog.done(2))
            delete = box.addButton('删除', QDialogButtonBox.ButtonRole.DestructiveRole)

            def remove():
                if QMessageBox.question(dialog, '删除待办', '确定删除此待办？') == QMessageBox.StandardButton.Yes:
                    self.data.remove('tasks', task['id'])
                    dialog.reject()

            delete.clicked.connect(remove)
        result = dialog.exec()
        if result in (QDialog.DialogCode.Accepted, 2) and title.text().strip():
            task = self.data.write('tasks', dict(task, title=title.text().strip(), due=due.date().toPython().isoformat(),
                                                priority=priority.currentData(), done=done.isChecked(),
                                                event_id=event.currentData()))
            if result == 2:
                self.new_event(day=date.fromisoformat(task['due']), task=task)
        self.refresh()

    def tasks_dialog(self):
        dialog = QDialog(self)
        dialog.setWindowTitle('待办清单')
        dialog.resize(545, 420)
        layout = QVBoxLayout(dialog)
        listing = QListWidget()
        layout.addWidget(listing)

        def populate():
            listing.clear()
            tasks = sorted(self.data.records('tasks'), key=lambda item: (item['done'], item['due'], item['title']))
            for task in tasks:
                item = QListWidgetItem(
                    ('✓ ' if task['done'] else '□ ') + f'{task["due"]} · {task["priority"]} · {task["title"]}')
                item.setData(ROLE, task)
                listing.addItem(item)

        def open_task(item=None):
            self.edit_task(task=item.data(ROLE) if item else None)
            populate()

        listing.itemActivated.connect(open_task)
        button('＋ 新建待办', layout, lambda: open_task())
        populate()
        dialog.exec()

    def apply_appearance(self):
        font = self.data.setting('font', '')
        if font:
            self.setFont(QFont(font))
        theme = self.data.setting('theme', '浅绿')
        background, focus = {'浅绿': ('#f5faf0', '#79b06b'), '浅蓝': ('#f0f7fc', '#436e97'),
                             '暖色': ('#fff8ef', '#98693e')}[theme]
        self.setStyleSheet(
            f'QTableWidget, QCalendarWidget QAbstractItemView, QListWidget {{ background: {background}; }}'
            f'QCalendarWidget QAbstractItemView {{ selection-background-color: {focus}; color: #243b30; }}')
        self.today_items.setVisible(self.data.setting('day_widget', True))

    def settings(self):
        dialog = QDialog(self)
        dialog.setWindowTitle('日历设置')
        form = QFormLayout(dialog)
        calendars = self.data.calendars()
        controls = {}
        for ident, row in calendars.items():
            same_row = QHBoxLayout()
            shown = QCheckBox('显示')
            shown.setChecked(bool(row['visible']))
            color = QPushButton()
            choose_color(color, row['color'])
            same_row.addWidget(shown)
            same_row.addWidget(color)
            form.addRow(row['name'], same_row)
            controls[ident] = shown, color
        default = choice((ident, row['name']) for ident, row in calendars.items())
        default.setCurrentIndex(default.findData(self.data.setting('default', 1)))
        official = QCheckBox('显示中国大陆放假与调休（2026–2027）')
        official.setChecked(self.data.setting('official', True))
        subscription = QLineEdit(self.data.setting('subscription_url', ''))
        subscription.setPlaceholderText('https://…/calendar.ics')
        theme = choice((key, key) for key in ('浅绿', '浅蓝', '暖色'))
        theme.setCurrentIndex(theme.findData(self.data.setting('theme', '浅绿')))
        font = QFontComboBox()
        font.setCurrentFont(QFont(self.data.setting('font', self.font().family())))
        widget = QCheckBox('显示当天日程与待办小组件')
        widget.setChecked(self.data.setting('day_widget', True))
        for caption, field in [('默认日历', default), ('节假日', official), ('节假日日历订阅', subscription),
                               ('主题', theme), ('字体', font), ('小组件', widget)]:
            form.addRow(caption, field)
        box = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        box.accepted.connect(dialog.accept)
        box.rejected.connect(dialog.reject)
        form.addRow(box)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        address = subscription.text().strip()
        if address and (QUrl(address).scheme() != 'https' or not QUrl(address).host()):
            QMessageBox.warning(self, '订阅地址', '请输入完整的 HTTPS 日历订阅地址。')
            return
        for ident, (shown, color) in controls.items():
            self.data.update_calendar(ident, color.value, shown.isChecked())
        for key, value in [('default', default.currentData()), ('official', official.isChecked()),
                           ('theme', theme.currentData()), ('font', font.currentFont().family()),
                           ('day_widget', widget.isChecked())]:
            self.data.set_setting(key, value)
        previous = self.data.setting('subscription_url', '')
        if previous != address:
            self.data.set_setting('subscription_ics', '')
            self.subscription = None
        self.data.set_setting('subscription_url', address)
        if address:
            self.fetch_subscription(interactive=True)
        self.apply_appearance()
        self.refresh()

    def load_subscription(self):
        from icalendar import Calendar

        payload = self.data.setting('subscription_ics', '')
        if payload:
            try:
                self.subscription = Calendar.from_ical(payload)
            except ValueError:
                self.subscription = None

    def fetch_subscription(self, interactive=False):
        from icalendar import Calendar

        address = self.data.setting('subscription_url', '')
        if not address:
            return
        request = QNetworkRequest(QUrl(address))
        request.setTransferTimeout(12000)
        request.setAttribute(QNetworkRequest.Attribute.RedirectPolicyAttribute,
                             QNetworkRequest.RedirectPolicy.SameOriginRedirectPolicy)
        reply = self.network.get(request)
        limit = 2 * 1024 * 1024

        def limit_response():
            if reply.bytesAvailable() > limit:
                reply.abort()

        def finished():
            try:
                if self.data.setting('subscription_url', '') != address:
                    return
                if reply.error() != QNetworkReply.NetworkError.NoError:
                    raise ValueError(reply.errorString())
                payload = bytes(reply.readAll())
                if len(payload) > limit:
                    raise ValueError('订阅超过 2 MB')
                parsed = Calendar.from_ical(payload)
                self.data.set_setting('subscription_ics', payload.decode('utf-8-sig'))
                self.subscription = parsed
                self.refresh()
            except (ValueError, UnicodeError) as error:
                if interactive:
                    QMessageBox.warning(self, '订阅刷新失败', str(error))
            finally:
                reply.deleteLater()

        reply.readyRead.connect(limit_response)
        reply.finished.connect(finished)

    def check_notifications(self):
        now = datetime.now()
        for row in self.data.collect_reminders(now):
            event = self.data.reminder_event(row)
            if not event:
                self.data.reminder_action(row, now)
                continue
            self.owner.tray.showMessage('日程提醒 · ' + event['title'],
                                        '打开提醒中心可稍后提醒或标为已读。')
            if event['repeat_notify']:
                self.data.reminder_action(row, now, event['repeat_notify'])
            else:
                self.data.mark_notified(row)

    def notifications(self):
        dialog = QDialog(self)
        dialog.setWindowTitle('提醒中心')
        dialog.resize(480, 320)
        layout = QVBoxLayout(dialog)
        listing = QListWidget()
        layout.addWidget(listing)
        for row in self.data.unread_reminders():
            event = self.data.reminder_event(row)
            if not event:
                continue
            item = QListWidgetItem(row['occurrence'].replace('T', ' ') + ' · ' + event['title'])
            item.setData(ROLE, row)
            listing.addItem(item)

        def action(snooze):
            item = listing.currentItem()
            if not item:
                return
            minutes = None
            if snooze:
                minutes, ok = QInputDialog.getInt(dialog, '稍后提醒', '分钟', 10, 1, 1440)
                if not ok:
                    return
            self.data.reminder_action(item.data(ROLE), datetime.now(), minutes)
            listing.takeItem(listing.currentRow())

        button('稍后提醒', layout, lambda: action(True))
        button('标为已读并停止提醒', layout, lambda: action(False))
        dialog.exec()
