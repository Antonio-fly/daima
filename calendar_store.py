"""Local calendar records, recurring occurrences and reminder state."""
import json
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from functools import lru_cache
from uuid import uuid4

DAY = timedelta(days=1)
MINUTE = timedelta(minutes=1)
RULES = {
    'none': '不重复', 'daily': '每天', 'weekly': '每周', 'monthly': '每月',
    'yearly': '每年', 'weekdays': '工作日', 'biweekly': '每两周', 'nth': '每月同序号星期',
}
CALENDARS = [('工作', '#276a9b'), ('个人', '#6e4e9e'), ('家庭', '#9a562c'), ('学习', '#38714e')]
# Annual State Council schedules. Dates outside these years have no official override.
HOLIDAY_RANGES = {
    2026: [('元旦', 1, 1, 3), ('春节', 2, 15, 23), ('清明节', 4, 4, 6),
           ('劳动节', 5, 1, 5), ('端午节', 6, 19, 21), ('中秋节', 9, 25, 27), ('国庆节', 10, 1, 7)],
    2027: [('元旦', 1, 1, 3), ('春节', 2, 5, 13), ('清明节', 4, 3, 5),
           ('劳动节', 5, 1, 5), ('端午节', 6, 9, 9), ('中秋节', 9, 15, 15), ('国庆节', 10, 1, 7)],
}
MAKEUP = {
    2026: [(1, 4), (2, 14), (2, 28), (5, 9), (9, 20), (10, 10)],
    2027: [(1, 31), (2, 14), (5, 8), (9, 26), (10, 9)],
}


def official_day(day):
    if (day.month, day.day) in MAKEUP.get(day.year, ()):
        return '调休上班'
    for title, month, first, last in HOLIDAY_RANGES.get(day.year, ()):
        if day.month == month and first <= day.day <= last:
            return title + '·休'
    return ''


def workday(day):
    marking = official_day(day)
    return marking == '调休上班' if marking else day.weekday() < 5


@lru_cache(maxsize=730)
def lunar_labels(day):
    from lunar_python import Solar

    lunar = Solar.fromYmd(day.year, day.month, day.day).getLunar()
    lunar_day = lunar.getDayInChinese()
    if lunar.getDay() == 1:
        lunar_day = lunar.getMonthInChinese() + '月'
    festivals = lunar.getFestivals() + lunar.getSolar().getFestivals()
    return lunar_day, lunar.getJieQi(), '、'.join(dict.fromkeys(festivals))


@dataclass(frozen=True)
class Occurrence:
    event: dict
    start: datetime
    end: datetime

    @property
    def key(self):
        return self.event['id'], self.start.isoformat(timespec='minutes')


def matches(day, anchor, rule):
    distance = (day - anchor).days
    if rule == 'weekdays':
        return workday(day)
    if distance == 0 or rule == 'daily':
        return True
    if rule in ('weekly', 'biweekly'):
        return distance % (7 if rule == 'weekly' else 14) == 0
    if rule == 'yearly':
        return (day.month, day.day) == (anchor.month, anchor.day)
    if rule == 'monthly':
        return day.day == anchor.day
    if rule == 'nth':
        return day.weekday() == anchor.weekday() and (day.day - 1) // 7 == (anchor.day - 1) // 7
    return False


def expand(event, since, until):
    start = datetime.fromisoformat(event['start'])
    end = datetime.fromisoformat(event['end'])
    if start >= until:
        return
    duration = end - start
    rule = event['rule']
    if rule == 'none':
        if end > since:
            yield Occurrence(event, start, end)
        return
    last = (until - timedelta(microseconds=1)).date()
    if event['repeat_until']:
        last = min(last, date.fromisoformat(event['repeat_until']))
    limit = event['repeat_count']
    seek = max(start.date(), (since - duration).date())
    day = start.date() if limit else seek
    count = 0
    while day <= last:
        if matches(day, start.date(), rule):
            count += 1
            current = datetime.combine(day, start.time())
            if day >= seek and current + duration > since:
                yield Occurrence(event, current, current + duration)
            if limit and count >= limit:
                break
        day += DAY


class CalendarStore:
    def __init__(self, connection):
        self.db = connection
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS calendars (
                id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL, color TEXT NOT NULL,
                visible INTEGER NOT NULL DEFAULT 1);
            CREATE TABLE IF NOT EXISTS calendar_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS calendar_events (id TEXT PRIMARY KEY, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS calendar_overrides (
                event_id TEXT NOT NULL, occurrence TEXT NOT NULL, payload TEXT,
                PRIMARY KEY (event_id, occurrence));
            CREATE TABLE IF NOT EXISTS calendar_reminders (
                event_id TEXT NOT NULL, occurrence TEXT NOT NULL, next_at TEXT NOT NULL,
                notified INTEGER NOT NULL DEFAULT 0, dismissed INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (event_id, occurrence));
            CREATE TABLE IF NOT EXISTS calendar_tasks (id TEXT PRIMARY KEY, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS calendar_templates (id TEXT PRIMARY KEY, payload TEXT NOT NULL);
        """)
        with self.db:
            for ident, (name, color) in enumerate(CALENDARS, 1):
                self.db.execute('INSERT OR IGNORE INTO calendars(id,name,color) VALUES(?,?,?)', (ident, name, color))

    def setting(self, key, default):
        row = self.db.execute('SELECT value FROM calendar_settings WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set_setting(self, key, value):
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO calendar_settings VALUES(?,?)', (key, json.dumps(value)))

    def calendars(self):
        return {row['id']: dict(row) for row in self.db.execute('SELECT * FROM calendars ORDER BY id')}

    def update_calendar(self, ident, color, visible):
        with self.db:
            self.db.execute('UPDATE calendars SET color=?,visible=? WHERE id=?', (color, visible, ident))

    def records(self, kind):
        table = {'events': 'calendar_events', 'tasks': 'calendar_tasks', 'templates': 'calendar_templates'}[kind]
        return [json.loads(row[0]) for row in self.db.execute(f'SELECT payload FROM {table}')]

    def write(self, kind, record):
        table = {'events': 'calendar_events', 'tasks': 'calendar_tasks', 'templates': 'calendar_templates'}[kind]
        record = dict(record)
        record.setdefault('id', uuid4().hex)
        with self.db:
            self.db.execute(f'INSERT OR REPLACE INTO {table} VALUES(?,?)', (record['id'], json.dumps(record)))
            if kind == 'events':
                self.db.execute('DELETE FROM calendar_reminders WHERE event_id=?', (record['id'],))
        return record

    def merge_events(self, record, removed_id):
        retained_id = record['id']
        with self.db:
            if removed_id != retained_id:
                for task in self.records('tasks'):
                    if task['event_id'] == removed_id:
                        task['event_id'] = retained_id
                        self.db.execute('UPDATE calendar_tasks SET payload=? WHERE id=?',
                                        (json.dumps(task), task['id']))
                self.db.execute('DELETE FROM calendar_events WHERE id=?', (removed_id,))
            self.db.execute('INSERT OR REPLACE INTO calendar_events VALUES(?,?)',
                            (retained_id, json.dumps(record)))
            self.db.execute('DELETE FROM calendar_reminders WHERE event_id IN (?,?)', (retained_id, removed_id))
        return record

    def remove(self, kind, ident):
        table = {'events': 'calendar_events', 'tasks': 'calendar_tasks', 'templates': 'calendar_templates'}[kind]
        with self.db:
            self.db.execute(f'DELETE FROM {table} WHERE id=?', (ident,))
            if kind == 'events':
                self.db.execute('DELETE FROM calendar_overrides WHERE event_id=?', (ident,))
                self.db.execute('DELETE FROM calendar_reminders WHERE event_id=?', (ident,))

    def override(self, occurrence, replacement=None):
        key = occurrence.event.get('_original_key', occurrence.key[1])
        ident = occurrence.event['id']
        if replacement is not None:
            replacement = dict(replacement, id=ident, rule='none', _original_key=key)
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO calendar_overrides VALUES(?,?,?)',
                            (ident, key, json.dumps(replacement) if replacement else None))
            self.db.execute('DELETE FROM calendar_reminders WHERE event_id=? AND occurrence=?',
                            (ident, key))

    def occurrences(self, since, until, visible_only=True):
        visibility = self.calendars()
        overrides = {(row['event_id'], row['occurrence']): row['payload']
                     for row in self.db.execute('SELECT * FROM calendar_overrides')}
        results = []
        for event in self.records('events'):
            if not visible_only or visibility[event['calendar']]['visible']:
                results.extend(o for o in expand(event, since, until) if o.key not in overrides)
        for payload in overrides.values():
            if payload:
                event = json.loads(payload)
                if not visible_only or visibility[event['calendar']]['visible']:
                    results.extend(expand(event, since, until))
        return sorted(results, key=lambda o: (o.start, o.end, o.event['title']))

    def reminder_key(self, occurrence):
        return occurrence.event['id'], occurrence.event.get('_original_key', occurrence.key[1])

    def collect_reminders(self, now):
        """Persist each reminder instance so reopening the app cannot re-notify it."""
        stamp = now.isoformat(timespec='minutes')
        floor = now - DAY
        with self.db:
            for item in self.occurrences(floor, now + 31 * DAY, visible_only=False):
                offset = item.event['notify']
                if offset < 0:
                    continue
                trigger = item.start - offset * MINUTE
                if floor <= trigger <= now:
                    self.db.execute('INSERT OR IGNORE INTO calendar_reminders VALUES(?,?,?,0,0)',
                                    (*self.reminder_key(item), trigger.isoformat(timespec='minutes')))
            return [dict(row) for row in self.db.execute(
                'SELECT * FROM calendar_reminders WHERE dismissed=0 AND notified=0 AND next_at<=?', (stamp,))]

    def reminder_action(self, row, now, minutes=None):
        with self.db:
            if minutes is None:
                self.db.execute('UPDATE calendar_reminders SET notified=1,dismissed=1 WHERE event_id=? AND occurrence=?',
                                (row['event_id'], row['occurrence']))
            else:
                self.db.execute('UPDATE calendar_reminders SET notified=0,next_at=? WHERE event_id=? AND occurrence=?',
                                ((now + minutes * MINUTE).isoformat(timespec='minutes'),
                                 row['event_id'], row['occurrence']))

    def mark_notified(self, row):
        with self.db:
            self.db.execute('UPDATE calendar_reminders SET notified=1 WHERE event_id=? AND occurrence=?',
                            (row['event_id'], row['occurrence']))

    def unread_reminders(self):
        return [dict(row) for row in self.db.execute(
            'SELECT * FROM calendar_reminders WHERE dismissed=0 AND (notified=1 OR next_at>?) ORDER BY occurrence',
            (datetime.now().isoformat(timespec='minutes'),))]

    def reminder_event(self, row):
        override = self.db.execute('SELECT payload FROM calendar_overrides WHERE event_id=? AND occurrence=?',
                                   (row['event_id'], row['occurrence'])).fetchone()
        if override:
            return json.loads(override[0]) if override[0] else None
        event = self.db.execute('SELECT payload FROM calendar_events WHERE id=?', (row['event_id'],)).fetchone()
        return json.loads(event[0]) if event else None


def bounds(day):
    return datetime.combine(day, time.min), datetime.combine(day + DAY, time.min)
