# -*- coding: utf-8 -*-
"""牛马便签 — Windows desktop organizer (PySide6 + SQLite)."""
import os
import sys
import sqlite3
import json
import re
import time
import uuid
import calendar
import shutil
import ctypes
from pathlib import Path
from datetime import datetime, date
from html import escape

from PySide6.QtCore import Qt, QDate, QDateTime, QTimer, QSize, QEvent, QObject, QAbstractNativeEventFilter, Signal
from PySide6.QtGui import (QIcon, QColor, QFont, QTextCharFormat, QTextCursor, QTextListFormat, QPainter,
                           QAction, QKeySequence, QTextImageFormat, QGuiApplication)
from PySide6.QtWidgets import (QApplication, QMainWindow, QWidget, QFrame, QHBoxLayout, QVBoxLayout,
    QGridLayout, QPushButton, QLabel, QLineEdit, QTextEdit, QListWidget, QListWidgetItem,
    QCalendarWidget, QSplitter, QComboBox, QMenu, QMessageBox, QColorDialog, QInputDialog,
    QFileDialog, QSystemTrayIcon, QStyle, QToolBar, QDialog, QDialogButtonBox, QFormLayout,
    QSpinBox, QSlider, QCheckBox, QScrollArea, QDateTimeEdit, QStackedWidget, QFontComboBox,
    QTextBrowser, QGraphicsOpacityEffect, QDateEdit)

APP_NAME = '牛马便签'
CATEGORIES = [('工作','#33a2e6'),('生活','#43ad5c'),('学习','#9d65dd'),('待办','#f9ac23'),('灵感','#ef7094'),('个人成长','#f1c841')]
NOTE_COLORS = ['#d7eafe','#ffebaa','#ffd3df','#dcf4db','#f0dcff','#fff9db','#f5f8ed']


def asset(filename):
    base = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent))
    return str(base / 'assets' / filename)


def data_dir():
    root = Path(os.environ.get('APPDATA', str(Path.home()))) / 'NiuMaNotes'
    old_root = root.parent / 'QingNingNotes'
    if not root.exists() and old_root.exists():
        shutil.copytree(old_root, root)
    root.mkdir(parents=True, exist_ok=True)
    (root / 'images').mkdir(exist_ok=True)
    (root / 'attachments').mkdir(exist_ok=True)
    return root


DB = data_dir() / 'notes.db'


class Store:
    def __init__(self):
        self.conn = sqlite3.connect(DB)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute('PRAGMA journal_mode=WAL')
        self.conn.executescript('''
        CREATE TABLE IF NOT EXISTS notes (
          id TEXT PRIMARY KEY, title TEXT NOT NULL DEFAULT '', html TEXT NOT NULL DEFAULT '',
          content TEXT NOT NULL DEFAULT '', category TEXT NOT NULL DEFAULT '工作',
          tags TEXT NOT NULL DEFAULT '', color TEXT NOT NULL DEFAULT '#d7eafe', opacity INTEGER NOT NULL DEFAULT 94,
          pinned INTEGER NOT NULL DEFAULT 0, archived INTEGER NOT NULL DEFAULT 0,
          trashed INTEGER NOT NULL DEFAULT 0, reminder TEXT NOT NULL DEFAULT '',
          reminded INTEGER NOT NULL DEFAULT 0, x INTEGER, y INTEGER, w INTEGER DEFAULT 335, h INTEGER DEFAULT 300,
          created TEXT NOT NULL, updated TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS files (
          id TEXT PRIMARY KEY, note_id TEXT NOT NULL, name TEXT NOT NULL, path TEXT NOT NULL,
          FOREIGN KEY(note_id) REFERENCES notes(id));
        CREATE INDEX IF NOT EXISTS idx_notes_updated ON notes(updated);
        ''')
        # Preserve existing databases. Existing notes keep their original creation day,
        # never the last-edit day, as their initial calendar association.
        columns={row[1] for row in self.conn.execute('PRAGMA table_info(notes)')}
        if 'note_date' not in columns:
            self.conn.execute("ALTER TABLE notes ADD COLUMN note_date TEXT NOT NULL DEFAULT ''")
            self.conn.execute("UPDATE notes SET note_date=substr(created,1,10) WHERE note_date=''")
            self.conn.commit()
        self.conn.execute("UPDATE notes SET note_date=substr(created,1,10) WHERE note_date=''")
        self.conn.commit()

    def add(self, title='新便签', category='工作', color=None, note_date=None):
        nid = uuid.uuid4().hex
        now = datetime.now().isoformat(timespec='seconds')
        chosen=note_date or now[:10]
        self.conn.execute('INSERT INTO notes(id,title,category,color,note_date,created,updated) VALUES(?,?,?,?,?,?,?)',
                          (nid,title,category,color or NOTE_COLORS[len(self.all()) % len(NOTE_COLORS)],chosen,now,now))
        self.conn.commit()
        return nid

    def get(self, nid):
        r = self.conn.execute('SELECT * FROM notes WHERE id=?',(nid,)).fetchone()
        return dict(r) if r else None

    def all(self, mode='active', category=None, query=''):
        sql = 'SELECT * FROM notes WHERE 1=1'
        vals = []
        sql += {'active':' AND trashed=0 AND archived=0','archive':' AND archived=1 AND trashed=0',
                'trash':' AND trashed=1','all':' AND trashed=0'}.get(mode,'')
        if category and category != '全部':
            sql += ' AND category=?'; vals.append(category)
        if query.strip():
            sql += ' AND (title LIKE ? OR content LIKE ? OR tags LIKE ?)'
            vals.extend(['%'+query+'%']*3)
        sql += ' ORDER BY pinned DESC, updated DESC'
        return [dict(r) for r in self.conn.execute(sql,vals)]

    def update(self, nid, **fields):
        valid = {'title','html','content','category','tags','color','opacity','pinned','archived','trashed','note_date',
                 'reminder','reminded','x','y','w','h'}
        fields = {k:v for k,v in fields.items() if k in valid}
        if not fields: return
        fields['updated'] = datetime.now().isoformat(timespec='seconds')
        self.conn.execute('UPDATE notes SET '+','.join(k+'=?' for k in fields)+' WHERE id=?',
                          [*fields.values(),nid]); self.conn.commit()

    def destroy(self,nid):
        self.conn.execute('DELETE FROM files WHERE note_id=?',(nid,))
        self.conn.execute('DELETE FROM notes WHERE id=?',(nid,)); self.conn.commit()

    def file_add(self,nid,path):
        dest = data_dir()/'attachments'/(uuid.uuid4().hex+'_'+Path(path).name)
        shutil.copy2(path,dest)
        self.conn.execute('INSERT INTO files VALUES(?,?,?,?)',(uuid.uuid4().hex,nid,Path(path).name,str(dest)))
        self.conn.commit()

    def files(self,nid):
        return [dict(x) for x in self.conn.execute('SELECT * FROM files WHERE note_id=?',(nid,))]


class HotkeyFilter(QAbstractNativeEventFilter):
    """RegisterHotKey -> WM_HOTKEY; Windows prevents duplicate key delivery."""
    triggered = None
    def nativeEventFilter(self, eventType, message):
        if sys.platform != 'win32' or eventType not in (b'windows_generic_MSG', b'windows_dispatcher_MSG'): return False, 0
        from ctypes import wintypes
        class MSG(ctypes.Structure):
            _fields_ = [('hwnd',wintypes.HWND),('message',wintypes.UINT),('wParam',wintypes.WPARAM),
                        ('lParam',wintypes.LPARAM),('time',wintypes.DWORD),('pt',wintypes.POINT)]
        m = MSG.from_address(int(message))
        if m.message == 0x0312 and m.wParam in (0xB120, 0xB121):
            if self.triggered: self.triggered(int(m.wParam))
            return True,0
        return False,0


class Sticky(QWidget):
    """Independently movable, resizable floating sticky note."""
    def __init__(self, owner, nid):
        super().__init__(None, Qt.WindowType.Tool)
        self.owner, self.nid = owner,nid
        n = owner.store.get(nid)
        if not n: return
        self.setWindowTitle(n['title'])
        self.setMinimumSize(240,180)
        self.resize(n['w'] or 335,n['h'] or 300)
        if n['x'] is not None and n['y'] is not None: self.move(n['x'],n['y'])
        self.setWindowIcon(owner.icon)
        self.setWindowOpacity(max(.35,n['opacity']/100))
        self.dragpos = None
        self.edge = False
        self.normal_geometry = None
        lay = QVBoxLayout(self); lay.setContentsMargins(10,10,10,10)
        self.panel = QFrame(); self.panel.setObjectName('stickyPanel'); lay.addWidget(self.panel)
        inner = QVBoxLayout(self.panel); inner.setContentsMargins(10,8,10,8)
        hdr = QHBoxLayout(); inner.addLayout(hdr)
        self.title = QLineEdit(n['title']); self.title.setFrame(False); self.title.setStyleSheet('font-weight:700;font-size:15px;background:transparent;')
        hdr.addWidget(self.title)
        for symbol,slot,tip in [('📌',self.toggle_top,'置顶'),('▢',self.open_main,'在主窗口编辑'),('×',self.hide,'隐藏便签')]:
            b=QPushButton(symbol); b.setFixedSize(27,27); b.setToolTip(tip);b.clicked.connect(slot);hdr.addWidget(b)
        self.text = QTextEdit(); self.text.setAcceptRichText(True); self.text.setHtml(n['html'] or '')
        self.text.setStyleSheet('background:transparent;border:0;font-size:14px;');inner.addWidget(self.text)
        self.tip = QLabel('拖动标题栏移动 · 拖动窗口边框调整大小'); self.tip.setStyleSheet('color:#5c6e64;font-size:10px;');inner.addWidget(self.tip)
        self.title.editingFinished.connect(self.save)
        self.text.textChanged.connect(self.debounce)
        self.timer=QTimer(self);self.timer.setSingleShot(True);self.timer.timeout.connect(self.save)
        self.refresh_color()

    def debounce(self): self.timer.start(700)
    def refresh_color(self):
        n=self.owner.store.get(self.nid)
        if n:self.panel.setStyleSheet(f'#stickyPanel{{background:{n["color"]};border-radius:13px;}}')
    def save(self):
        if self.owner.store.get(self.nid):
            self.owner.store.update(self.nid,title=self.title.text(),html=self.text.toHtml(),content=self.text.toPlainText(),
                                    x=self.x(),y=self.y(),w=self.width(),h=self.height())
            self.owner.reload_list(keep=True)
    def toggle_top(self):
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint,not bool(self.windowFlags() & Qt.WindowType.WindowStaysOnTopHint))
        self.show()
    def open_main(self):
        self.owner.show_normal();self.owner.select_id(self.nid)
    def moveEvent(self,e):
        if hasattr(self,'timer'):self.timer.start(800)
        super().moveEvent(e)
    def resizeEvent(self,e):
        if hasattr(self,'timer'):self.timer.start(800)
        super().resizeEvent(e)
    def enterEvent(self,e):
        if self.edge and self.normal_geometry:
            self.setGeometry(self.normal_geometry);self.edge=False
        super().enterEvent(e)
    def leaveEvent(self,e):
        # Auto-hide when touching a side of the current screen.
        if not self.isVisible() or self.edge: return
        g = self.screen().availableGeometry() if self.screen() else None
        if not g:return
        r=self.geometry();nx=None
        if r.x()<=g.x()+6:nx=g.x()-r.width()+18
        elif r.right()>=g.right()-6:nx=g.right()-18
        if nx is not None:
            self.normal_geometry=r
            self.edge=True
            self.move(nx,r.y())
        super().leaveEvent(e)
    def closeEvent(self,e):
        self.save();self.owner.stickies.pop(self.nid,None);super().closeEvent(e)


class NoteTextEdit(QTextEdit):
    """Paste screenshots into persistent image files and toggle ☐/☑ on double click."""
    def __init__(self, parent=None):
        super().__init__(parent)

    def insertFromMimeData(self, source):
        if source.hasImage():
            im = source.imageData()
            if not im.isNull():
                path = data_dir()/'images'/(uuid.uuid4().hex+'.png')
                im.save(str(path), 'PNG')
                self.insertHtml('<img src="%s" width="340" />' % escape(path.as_uri(), quote=True))
                return
        super().insertFromMimeData(source)

    def mouseDoubleClickEvent(self, event):
        cursor = self.cursorForPosition(event.position().toPoint())
        block = cursor.block()
        txt = block.text()
        if txt.startswith('☐ ') or txt.startswith('☑ '):
            completed = txt.startswith('☐ ')
            cursor.setPosition(block.position())
            cursor.movePosition(QTextCursor.MoveOperation.Right, QTextCursor.MoveMode.KeepAnchor)
            cursor.insertText('☑' if completed else '☐')
            cursor.select(QTextCursor.SelectionType.BlockUnderCursor)
            fmt = QTextCharFormat();fmt.setFontStrikeOut(completed)
            cursor.mergeCharFormat(fmt)
            return
        super().mouseDoubleClickEvent(event)


class Editor(QWidget):
    def __init__(self,owner):
        super().__init__();self.owner=owner;self.nid=None;self.loading=False
        root=QVBoxLayout(self);root.setContentsMargins(14,10,14,12);root.setSpacing(8)
        h=QHBoxLayout();root.addLayout(h)
        self.title=QLineEdit();self.title.setPlaceholderText('便签标题');self.title.setObjectName('noteTitle');h.addWidget(self.title)
        self.pin=QPushButton('☆ 收藏');self.pin.clicked.connect(self.toggle_pin);h.addWidget(self.pin)
        self.float_btn=QPushButton('↗ 桌面便签');self.float_btn.clicked.connect(self.float_note);h.addWidget(self.float_btn)
        self.archive=QPushButton('归档');self.archive.clicked.connect(owner.archive_current);h.addWidget(self.archive)
        self.restore=QPushButton('恢复');self.restore.clicked.connect(owner.restore_current);h.addWidget(self.restore)
        self.delete=QPushButton('删除');self.delete.clicked.connect(self.trash);h.addWidget(self.delete)
        meta=QHBoxLayout();root.addLayout(meta)
        self.category=QComboBox();self.category.addItems([x[0] for x in CATEGORIES]);meta.addWidget(QLabel('分类'));meta.addWidget(self.category)
        self.tags=QLineEdit();self.tags.setPlaceholderText('标签：多个用逗号分隔');meta.addWidget(self.tags,2)
        meta.addWidget(QLabel('事项日期'))
        self.note_date=QDateEdit(QDate.currentDate());self.note_date.setCalendarPopup(True)
        self.note_date.setDisplayFormat('yyyy-MM-dd');self.note_date.setToolTip('便签所属日期，修改后将在日历中同步移动')
        meta.addWidget(self.note_date)
        colorbtn=QPushButton('🎨 背景');colorbtn.clicked.connect(self.pick_color);meta.addWidget(colorbtn)
        meta.addWidget(QLabel('透明度'))
        self.opacity=QSlider(Qt.Orientation.Horizontal);self.opacity.setRange(35,100);self.opacity.setFixedWidth(78);meta.addWidget(self.opacity)
        bar=QHBoxLayout();root.addLayout(bar)
        def tool(label,fn,tip=''):
            b=QPushButton(label);b.setToolTip(tip);b.setFixedHeight(30);bar.addWidget(b);b.clicked.connect(fn);return b
        tool('B',lambda:self.format_toggle('bold'),'粗体');tool('I',lambda:self.format_toggle('italic'),'斜体')
        tool('U',lambda:self.format_toggle('underline'),'下划线')
        self.fontsize=QComboBox();self.fontsize.addItems(['10','12','14','16','18','20','24','28','32']);self.fontsize.setCurrentText('14');self.fontsize.currentTextChanged.connect(self.set_font_size);bar.addWidget(self.fontsize)
        tool('字色',self.text_color);tool('• 列表',lambda:self.make_list(QTextListFormat.Style.ListDisc))
        tool('1. 列表',lambda:self.make_list(QTextListFormat.Style.ListDecimal))
        tool('⇥',self.indent);tool('☑ 待办',self.insert_checkbox)
        tool('图片',self.insert_image);tool('附件',self.attach_file)
        self.body=NoteTextEdit();self.body.setAcceptRichText(True);self.body.setPlaceholderText('在这里记录灵感、任务与日常…\n可以 Ctrl+V 粘贴截图');self.body.setMinimumHeight(200)
        root.addWidget(self.body,1)
        reminder=QHBoxLayout();root.addLayout(reminder)
        self.reminder_on=QCheckBox('提醒');reminder.addWidget(self.reminder_on)
        self.reminder_at=QDateTimeEdit(QDateTime.currentDateTime());self.reminder_at.setCalendarPopup(True);self.reminder_at.setDisplayFormat('yyyy-MM-dd HH:mm');reminder.addWidget(self.reminder_at)
        self.files_btn=QPushButton('查看附件');self.files_btn.clicked.connect(self.show_files);reminder.addWidget(self.files_btn)
        reminder.addStretch(1)
        self.status=QLabel('自动保存已开启');root.addWidget(self.status)
        self.save_timer=QTimer(self);self.save_timer.setSingleShot(True);self.save_timer.timeout.connect(self.save)
        for w in [self.title,self.tags]:w.textChanged.connect(self.schedule)
        self.body.textChanged.connect(self.schedule)
        self.category.currentTextChanged.connect(self.schedule)
        self.opacity.valueChanged.connect(self.schedule)
        self.reminder_on.toggled.connect(self.schedule)
        self.reminder_at.dateTimeChanged.connect(self.schedule)
        self.note_date.dateChanged.connect(self.schedule)
        self.setEnabled(False)

    def schedule(self,*_):
        if not self.loading and self.nid:self.save_timer.start(450)
    def load_note(self,nid):
        if self.nid and self.nid!=nid:self.save()
        n=self.owner.store.get(nid)
        self.nid=nid if n else None
        self.setEnabled(bool(n))
        if not n:return
        self.loading=True
        self.archive.setVisible(not bool(n['archived'] or n['trashed']))
        self.restore.setVisible(bool(n['archived'] or n['trashed']))
        self.delete.setText('永久删除' if n['trashed'] else '删除')
        self.title.setText(n['title']);self.category.setCurrentText(n['category']);self.tags.setText(n['tags'])
        parsed_date=QDate.fromString(n['note_date'],'yyyy-MM-dd')
        self.note_date.setDate(parsed_date if parsed_date.isValid() else QDate.currentDate())
        self.body.setHtml(n['html'] or '');self.opacity.setValue(n['opacity']);self.pin.setText('★ 已收藏' if n['pinned'] else '☆ 收藏')
        self.reminder_on.setChecked(bool(n['reminder']))
        if n['reminder']:
            dt=QDateTime.fromString(n['reminder'],'yyyy-MM-dd HH:mm')
            if dt.isValid():self.reminder_at.setDateTime(dt)
        self.apply_color(n['color']);self.loading=False
        self.status.setText('自动保存已开启')

    def apply_color(self,color):self.body.setStyleSheet('QTextEdit{background:%s;border-radius:12px;padding:12px;color:#25352c;}'%color)
    def save(self):
        if self.loading or not self.nid or not self.owner.store.get(self.nid):return
        self.save_timer.stop()
        reminder=self.reminder_at.dateTime().toString('yyyy-MM-dd HH:mm') if self.reminder_on.isChecked() else ''
        current=self.owner.store.get(self.nid)
        self.owner.store.update(self.nid,title=self.title.text() or '无标题',html=self.body.toHtml(),
            content=self.body.toPlainText(),category=self.category.currentText(),tags=self.tags.text(),
            opacity=self.opacity.value(),note_date=self.note_date.date().toString('yyyy-MM-dd'),
            reminder=reminder,reminded=0 if reminder!=current['reminder'] else current['reminded'])
        self.status.setText('✓ 已保存 · '+datetime.now().strftime('%H:%M:%S'))
        self.owner.reload_list(keep=True);self.owner.refresh_calendar()
    def format_toggle(self,key):
        f=QTextCharFormat();cur=self.body.currentCharFormat()
        if key=='bold':f.setFontWeight(QFont.Weight.Normal if cur.fontWeight()==QFont.Weight.Bold else QFont.Weight.Bold)
        elif key=='italic':f.setFontItalic(not cur.fontItalic())
        else:f.setFontUnderline(not cur.fontUnderline())
        self.body.mergeCurrentCharFormat(f);self.body.setFocus()
    def set_font_size(self,s):
        if self.loading:return
        f=QTextCharFormat();f.setFontPointSize(float(s));self.body.mergeCurrentCharFormat(f)
    def text_color(self):
        c=QColorDialog.getColor(parent=self)
        if c.isValid():
            f=QTextCharFormat();f.setForeground(c);self.body.mergeCurrentCharFormat(f)
    def make_list(self,style):
        cur=self.body.textCursor();fmt=QTextListFormat();fmt.setStyle(style);cur.createList(fmt)
    def indent(self):
        cur=self.body.textCursor();fmt=cur.blockFormat();fmt.setIndent(fmt.indent()+1);cur.setBlockFormat(fmt)
    def insert_checkbox(self):
        # Native rich-text checkbox glyph; users can toggle it through the editor toolbar.
        self.body.insertHtml('<p>☐ 待办事项</p>')
    def insert_image(self):
        path,_=QFileDialog.getOpenFileName(self,'插入图片','','图片 (*.png *.jpg *.jpeg *.gif *.bmp)')
        if path:self.add_image(path)
    def add_image(self,path):
        dest=data_dir()/'images'/(uuid.uuid4().hex+Path(path).suffix.lower())
        shutil.copy2(path,dest)
        self.body.insertHtml('<img src="%s" width="340" />'%escape(dest.as_uri(),quote=True))
        self.schedule()
    def attach_file(self):
        if not self.nid:return
        path,_=QFileDialog.getOpenFileName(self,'添加附件')
        if path:self.owner.store.file_add(self.nid,path);self.status.setText('✓ 已添加附件 '+Path(path).name)
    def show_files(self):
        if not self.nid:return
        f=self.owner.store.files(self.nid)
        if not f:QMessageBox.information(self,'附件','当前便签暂无附件。');return
        labels=[x['name'] for x in f]
        v,ok=QInputDialog.getItem(self,'打开附件','选择附件',labels,0,False)
        if ok:
            from PySide6.QtGui import QDesktopServices
            from PySide6.QtCore import QUrl
            QDesktopServices.openUrl(QUrl.fromLocalFile(f[labels.index(v)]['path']))
    def pick_color(self):
        if not self.nid:return
        n=self.owner.store.get(self.nid)
        c=QColorDialog.getColor(QColor(n['color']),self,'选择便签背景色')
        if c.isValid():self.owner.store.update(self.nid,color=c.name());self.apply_color(c.name());self.owner.update_sticky(self.nid)
    def toggle_pin(self):
        if self.nid:
            n=self.owner.store.get(self.nid);self.owner.store.update(self.nid,pinned=0 if n['pinned'] else 1)
            self.pin.setText('★ 已收藏' if not n['pinned'] else '☆ 收藏');self.owner.reload_list(keep=True)
    def float_note(self):
        if self.nid:self.save();self.owner.open_sticky(self.nid)
    def trash(self):
        if self.nid:self.owner.move_to_trash(self.nid)


class NoteCalendar(QCalendarWidget):
    """Calendar with persistent visual markers based on note_date, not updated."""
    def __init__(self,parent=None):
        super().__init__(parent)
        self.note_counts={}

    def paintCell(self,painter,rect,day):
        super().paintCell(painter,rect,day)
        count=self.note_counts.get(day.toString('yyyy-MM-dd'),0)
        if not count:return
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing,True)
        # Small colored count badge in top right; remains visible on selected dates.
        badge=rect.adjusted(rect.width()-33,4,-4,-rect.height()+23)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor('#368b50'))
        painter.drawRoundedRect(badge,8,8)
        painter.setPen(QColor('#ffffff'))
        font=painter.font();font.setBold(True);font.setPointSize(8);painter.setFont(font)
        painter.drawText(badge,Qt.AlignmentFlag.AlignCenter,str(count) if count<100 else '99+')
        painter.restore()


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.store=Store();self.icon=QIcon(asset('icon.ico'))
        self.setWindowIcon(self.icon);self.setWindowTitle('牛马便签 · 日历与桌面笔记')
        self.resize(1290,785);self.setMinimumSize(1020,610)
        self.stickies={};self.hidden_stickies=[];self.mode='active';self.selected_category='全部';self.quit_real=False
        root=QWidget();root.setObjectName('root');self.setCentralWidget(root)
        main=QHBoxLayout(root);main.setContentsMargins(12,12,12,12);main.setSpacing(10)
        sidebar=QFrame();sidebar.setObjectName('sidebar');sidebar.setFixedWidth(225)
        main.addWidget(sidebar)
        sl=QVBoxLayout(sidebar);sl.setContentsMargins(14,17,14,14);sl.setSpacing(9)
        logo=QLabel('🐮  牛马便签');logo.setObjectName('logo');sl.addWidget(logo)
        motto=QLabel('让每一件小事都闪闪发光');motto.setStyleSheet('color:#547557');sl.addWidget(motto)
        self.search=QLineEdit();self.search.setPlaceholderText('🔎 搜索便签 / 标签 / 内容');self.search.textChanged.connect(self.reload_list);sl.addWidget(self.search)
        self.cat_buttons={}
        for label,callback in [('▦  日历视图',self.show_calendar),('▤  全部笔记',lambda:self.change_mode('active')),
            ('☆  星标收藏',self.favorites),('◷  最近使用',lambda:self.change_mode('active')),
            ('▣  归档',lambda:self.change_mode('archive')),('▤  回收站',lambda:self.change_mode('trash'))]:
            b=QPushButton(label);b.setObjectName('nav');b.clicked.connect(callback);sl.addWidget(b)
        sl.addSpacing(9);sl.addWidget(QLabel('我的分类'))
        for name,color in CATEGORIES:
            b=QPushButton('●  '+name);b.setObjectName('nav');b.setStyleSheet('text-align:left;color:'+color+';font-weight:700;')
            b.clicked.connect(lambda _,n=name:self.filter_category(n));sl.addWidget(b)
        sl.addStretch(1)
        new=QPushButton('＋  新建便签');new.setObjectName('primary');new.clicked.connect(self.new_note);sl.addWidget(new)
        settings=QPushButton('⚙  隐藏到系统托盘');settings.setObjectName('nav');settings.clicked.connect(self.hide_all);sl.addWidget(settings)
        self.stack=QStackedWidget();main.addWidget(self.stack,1)
        notespage=QWidget();self.stack.addWidget(notespage)
        nl=QVBoxLayout(notespage);nl.setContentsMargins(0,0,0,0)
        heading=QHBoxLayout();nl.addLayout(heading)
        self.heading=QLabel('全部笔记');self.heading.setObjectName('heading');heading.addWidget(self.heading);heading.addStretch()
        add=QPushButton('＋ 新建便签');add.setObjectName('primary');add.clicked.connect(self.new_note);heading.addWidget(add)
        splitter=QSplitter(Qt.Orientation.Horizontal);nl.addWidget(splitter,1)
        left=QFrame();left.setObjectName('noteListPanel');ll=QVBoxLayout(left);ll.setContentsMargins(10,10,10,10)
        self.list=QListWidget();self.list.setSpacing(5);self.list.currentItemChanged.connect(self.on_select);ll.addWidget(self.list)
        splitter.addWidget(left)
        self.editor=Editor(self);splitter.addWidget(self.editor)
        splitter.setSizes([250,720]);splitter.setStretchFactor(1,1)
        calpage=QWidget();self.stack.addWidget(calpage);cl=QVBoxLayout(calpage);cl.setContentsMargins(12,0,12,12)
        chead=QHBoxLayout();cl.addLayout(chead)
        title=QLabel('日历视图');title.setObjectName('heading');chead.addWidget(title);chead.addStretch()
        today=QPushButton('今天');today.clicked.connect(self.go_today);chead.addWidget(today)
        self.calendar=NoteCalendar();self.calendar.setGridVisible(True);self.calendar.setNavigationBarVisible(True)
        self.calendar.selectionChanged.connect(lambda:self.show_date_notes(self.calendar.selectedDate()))
        self.calendar.currentPageChanged.connect(lambda *_:self.refresh_calendar())
        cl.addWidget(self.calendar,2)
        daybar=QHBoxLayout();cl.addLayout(daybar)
        self.day_heading=QLabel('当日便签事项');daybar.addWidget(self.day_heading);daybar.addStretch()
        self.add_day=QPushButton('＋ 在所选日期新建便签');self.add_day.setObjectName('primary')
        self.add_day.clicked.connect(self.new_calendar_note);daybar.addWidget(self.add_day)
        self.calendar.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.calendar.customContextMenuRequested.connect(self.calendar_context_menu)
        self.day_notes=QListWidget();self.day_notes.itemDoubleClicked.connect(self.calendar_open_note);cl.addWidget(self.day_notes,1)
        self.tray=QSystemTrayIcon(self.icon,self);self.tray.setToolTip(APP_NAME+' · Ctrl+Alt+N 或双击 F12 唤醒/隐藏')
        menu=QMenu();a=menu.addAction('显示主窗口');a.triggered.connect(self.show_normal)
        b=menu.addAction('新建便签');b.triggered.connect(self.new_note)
        menu.addSeparator();a=menu.addAction('退出程序');a.triggered.connect(self.real_quit)
        self.tray.setContextMenu(menu);self.tray.activated.connect(self.tray_activated);self.tray.show()
        self.notice=QTimer(self);self.notice.timeout.connect(self.check_reminders);self.notice.start(20000)
        self.hotkey=HotkeyFilter();self.hotkey.triggered=self.on_hotkey;self.f12_last=0
        self.registered_hotkeys=[]
        if sys.platform=='win32':
            QApplication.instance().installNativeEventFilter(self.hotkey)
            user32=ctypes.windll.user32
            # MOD_NOREPEAT=0x4000; MOD_CONTROL=0x2; MOD_ALT=0x1
            # F12 is optional because browsers, debuggers and other utilities may claim it.
            for hotkey_id, modifiers, vk in ((0xB120,0x4000,0x7B),(0xB121,0x4000|0x2|0x1,0x4E)):
                if user32.RegisterHotKey(None,hotkey_id,modifiers,vk):
                    self.registered_hotkeys.append(hotkey_id)
            if 0xB120 not in self.registered_hotkeys:
                QTimer.singleShot(2500,lambda:self.tray.showMessage(APP_NAME,
                    'F12 热键不可用。请使用 Ctrl+Alt+N 唤醒或隐藏，或双击托盘图标。'))
            if not self.registered_hotkeys:
                QTimer.singleShot(3000,lambda:self.tray.showMessage(APP_NAME,
                    '全局热键均被占用，请从系统托盘双击图标或右键选择显示主窗口。'))
        self.style_app()
        if not self.store.all('all'):self.store.add('欢迎使用牛马便签','工作')
        self.reload_list();self.refresh_calendar()
        self.show_calendar()

    def style_app(self):
        self.setStyleSheet('''
        QWidget#root{background:#e9f1e6;color:#27382c;font-family:'Microsoft YaHei UI','Segoe UI';font-size:13px;}
        QFrame#sidebar{background:qlineargradient(x1:0,y1:0,x2:0.8,y2:1,stop:0 #c6e6b9,stop:1 #e4eed5);border-radius:18px;}
        QLabel#logo{font-size:23px;font-weight:800;color:#234b2b;}
        QLabel#heading{font-size:23px;font-weight:800;color:#21382b;}
        QPushButton{border:0;background:#ffffffad;border-radius:9px;padding:7px 10px;color:#2b422d;}
        QPushButton:hover{background:#c5e6bd;}
        QPushButton#nav{background:transparent;text-align:left;padding:10px 11px;}
        QPushButton#nav:hover{background:#a5cb97;border-radius:9px;}
        QPushButton#primary{background:#699e5a;color:white;font-weight:700;padding:10px 14px;}
        QPushButton#primary:hover{background:#477f3d;}
        QLineEdit,QComboBox,QDateTimeEdit{background:#ffffffc9;border:1px solid #d0e1cc;border-radius:8px;padding:8px;}
        QLineEdit#noteTitle{font-size:19px;font-weight:bold;}
        QFrame#noteListPanel{background:#f9fcf4;border-radius:14px;}
        QListWidget{background:#fafdf6;border:0;outline:0;}
        QListWidget::item{background:#e7f2e2;border-radius:10px;padding:12px;margin:3px;}
        QListWidget::item:selected{background:#b9dbaa;color:#1c4821;}
        QCalendarWidget{background:#f5faf0;border-radius:14px;}
        QCalendarWidget QAbstractItemView:enabled{background:#f5faf0;selection-background-color:#79b06b;selection-color:white;}
        QTextEdit{border:1px solid #d2e2cc;border-radius:10px;}
        ''')

    def show_calendar(self):self.stack.setCurrentIndex(1);self.refresh_calendar()
    def go_today(self):
        self.calendar.setCurrentPage(QDate.currentDate().year(),QDate.currentDate().month())
        self.calendar.setSelectedDate(QDate.currentDate())
        self.show_date_notes(self.calendar.selectedDate())
    def calendar_context_menu(self,point):
        menu=QMenu(self)
        menu.addAction('在选中日期新建便签',self.new_calendar_note)
        menu.exec(self.calendar.mapToGlobal(point))
    def new_calendar_note(self):
        self.new_note(note_date=self.calendar.selectedDate().toString('yyyy-MM-dd'))
    def change_mode(self,mode):
        self.mode=mode;self.selected_category='全部';self.heading.setText({'active':'全部笔记','archive':'归档','trash':'回收站'}[mode]);self.stack.setCurrentIndex(0);self.reload_list()
    def favorites(self):self.change_mode('active');self.reload_list(favorites=True)
    def filter_category(self,name):
        self.mode='active';self.selected_category=name;self.heading.setText(name+' · 便签');self.stack.setCurrentIndex(0);self.reload_list()
    def reload_list(self,*_,keep=False,favorites=False):
        cur=self.editor.nid if hasattr(self,'editor') else None
        if not hasattr(self,'list'):return
        self.list.blockSignals(True);self.list.clear()
        for n in self.store.all(self.mode,self.selected_category,self.search.text()):
            if favorites and not n['pinned']:continue
            label=('★ ' if n['pinned'] else '') + n['title']+'\n'+n['category']+' · '+n['updated'][:16].replace('T',' ')+'\n'+n['content'][:55].replace('\n',' ')
            item=QListWidgetItem(label);item.setData(Qt.ItemDataRole.UserRole,n['id']);self.list.addItem(item)
        index=next((i for i in range(self.list.count()) if self.list.item(i).data(Qt.ItemDataRole.UserRole)==cur),0)
        if self.list.count():self.list.setCurrentRow(index)
        else:self.editor.setEnabled(False);self.editor.nid=None
        self.list.blockSignals(False)
        if self.list.count() and not keep:self.on_select(self.list.currentItem())
    def on_select(self,current,*_):
        if current:self.editor.load_note(current.data(Qt.ItemDataRole.UserRole))
    def select_id(self,nid):
        self.change_mode('active')
        for i in range(self.list.count()):
            if self.list.item(i).data(Qt.ItemDataRole.UserRole)==nid:
                self.list.setCurrentRow(i);return
    def new_note(self,checked=False,note_date=None):
        # Qt clicked(bool) passes checked; ignore it to keep sidebar 'new' using today.
        selected=note_date if isinstance(note_date,str) else date.today().isoformat()
        category=self.selected_category if self.selected_category!='全部' else '工作'
        nid=self.store.add('新便签',category,note_date=selected);self.mode='active';self.selected_category='全部';self.stack.setCurrentIndex(0)
        self.reload_list();self.select_id(nid);self.editor.title.selectAll();self.editor.title.setFocus()
    def move_to_trash(self,nid):
        n=self.store.get(nid)
        if not n:return
        if n['trashed']:
            confirm=QMessageBox.question(self,'永久删除','确定永久删除？此操作不可撤销。')
            if confirm==QMessageBox.StandardButton.Yes:self.store.destroy(nid)
        else:self.store.update(nid,trashed=1)
        if nid in self.stickies:self.stickies[nid].close()
        self.editor.nid=None;self.reload_list()
    def keyPressEvent(self,event):
        if event.matches(QKeySequence.StandardKey.New):self.new_note()
        elif event.key()==Qt.Key.Key_Delete and self.mode=='trash' and self.list.hasFocus():self.move_to_trash(self.editor.nid)
        else:super().keyPressEvent(event)
    def contextMenuEvent(self,e):
        m=QMenu(self)
        for label,func in [('新建便签',self.new_note),('归档当前便签',self.archive_current),
                           ('恢复所选便签',self.restore_current),('删除所选便签',lambda:self.move_to_trash(self.editor.nid))]:
            m.addAction(label).triggered.connect(func)
        m.exec(e.globalPos())
    def archive_current(self):
        if self.editor.nid:self.store.update(self.editor.nid,archived=1);self.reload_list()
    def restore_current(self):
        if self.editor.nid:self.store.update(self.editor.nid,trashed=0,archived=0);self.reload_list()
    def open_sticky(self,nid):
        if nid in self.stickies:self.stickies[nid].show();self.stickies[nid].raise_();return
        n=Sticky(self,nid);self.stickies[nid]=n;n.show()
    def update_sticky(self,nid):
        if nid in self.stickies:self.stickies[nid].refresh_color()
    def refresh_calendar(self):
        counts={}
        for n in self.store.all('all'):
            day=n['note_date']
            counts[day]=counts.get(day,0)+1
        self.calendar.note_counts=counts
        self.calendar.updateCells()
        self.show_date_notes(self.calendar.selectedDate())

    def show_date_notes(self,qdate):
        if not hasattr(self,'day_notes'):return
        self.day_notes.clear()
        day=qdate.toString('yyyy-MM-dd')
        notes=[n for n in self.store.all('all') if n['note_date']==day]
        self.day_heading.setText(day+'  ·  '+str(len(notes))+' 条便签')
        for n in notes:
            it=QListWidgetItem(('★ ' if n['pinned'] else '📝 ')+n['title']+' · '+n['category'])
            it.setData(Qt.ItemDataRole.UserRole,n['id'])
            self.day_notes.addItem(it)
    def calendar_open_note(self,it):self.select_id(it.data(Qt.ItemDataRole.UserRole))
    def check_reminders(self):
        now=datetime.now().strftime('%Y-%m-%d %H:%M')
        for n in self.store.all('active'):
            if n['reminder'] and n['reminder']<=now and not n['reminded']:
                self.store.update(n['id'],reminded=1)
                self.tray.showMessage('⏰ '+n['title'],n['content'][:160] or '设定时间已到',QSystemTrayIcon.MessageIcon.Information,10000)
                QApplication.beep()
    def on_hotkey(self,hotkey_id):
        if hotkey_id==0xB121:
            self.f12_last=0
            self.toggle_show()
            return
        now=time.monotonic()
        if now-self.f12_last<=0.7:self.f12_last=0;self.toggle_show()
        else:self.f12_last=now
    def toggle_show(self):
        if self.isVisible():self.hide_all()
        else:self.show_normal()
    def hide_all(self):
        self.hidden_stickies=[nid for nid,note in self.stickies.items() if note.isVisible()]
        self.hide()
        for note in self.stickies.values():note.hide()
    def show_normal(self):
        self.show();self.showNormal();self.raise_();self.activateWindow()
        for nid in self.hidden_stickies:
            if nid in self.stickies:self.stickies[nid].show()
        self.hidden_stickies=[]
    def tray_activated(self,reason):
        if reason==QSystemTrayIcon.ActivationReason.DoubleClick:self.toggle_show()
    def closeEvent(self,e):
        self.editor.save()
        if not self.quit_real:
            e.ignore();self.hide_all();self.tray.showMessage(APP_NAME,'程序已隐藏到托盘。按 Ctrl+Alt+N 或连续按两次 F12 可唤醒。',QSystemTrayIcon.MessageIcon.Information,2500)
        else:e.accept()
    def real_quit(self):
        self.editor.save()
        for sticky in list(self.stickies.values()):sticky.close()
        self.quit_real=True
        if sys.platform=='win32':
            for hotkey_id in self.registered_hotkeys:
                ctypes.windll.user32.UnregisterHotKey(None,hotkey_id)
        self.tray.hide();QApplication.quit()


def main():
    if sys.platform=='win32':
        try:ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID('NiuMa.Notes.1')
        except Exception:pass
    app=QApplication(sys.argv);app.setQuitOnLastWindowClosed(False);app.setApplicationName(APP_NAME)
    app.setWindowIcon(QIcon(asset('icon.ico')))
    win=MainWindow();win.show()
    sys.exit(app.exec())


if __name__=='__main__':main()
