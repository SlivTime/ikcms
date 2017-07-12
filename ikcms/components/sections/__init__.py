import cPickle as pickle
import time

from sqlalchemy.orm import Query
from sqlalchemy import func

import ikcms.components.base
from ikcms.web import h_cases

from . import views


class Component(ikcms.components.base.Component):

    name = 'sections'
    model = 'front.Section'
    query_cls = Query
    check_timeout = 3
    lock_timeout = 3
    handler_updated_ts = None
    views = {
        'dir': views.DirView,
        'page': views.PageView,
    }

    def __init__(self, app):
        super(Component, self).__init__(app)
        self.model = self.app.db.get_model(self.model)
        self.cache_key_checked_ts = '{}.checked_ts'.format(self.name)
        self.cache_key_updated_ts = '{}.updated_ts'.format(self.name)
        self.cache_key_updating = '{}.updating'.format(self.name)
        self.cache_key_meta = '{}.meta'.format(self.name)
        self.cache_key_body = '{}.body'.format(self.name)
        self.cache_key_lock = '{}.lock'.format(self.name)
        self.init_cache()

    def on_request(self, request):
        # Update handler and root, if sections was updated
        cache_updated_ts = self.update_cache()
        if cache_updated_ts != self.handler_updated_ts:
            self.handler_updated_ts = cache_updated_ts
            self.app.handler = self.app.get_handler()
            self.app.root = self.app.get_root()

    def reset_cache(self, pipe):
        self.app.cache.delete(
            self.cache_key_updated_ts,
            self.cache_key_updating,
            self.cache_key_meta,
            self.cache_key_body,
        )

    def init_cache(self):
        with self.app.cache.lock(
            self.cache_key_lock,
            expires=self.lock_timeout,
        ) as lock:
            self.update_cache()

    def update_cache(self):
        # check update timeout
        cache_checked_ts, cache_updated_ts = self.app.cache.mget(
            self.cache_key_checked_ts,
            self.cache_key_updated_ts,
        )
        try:
            cache_checked_ts = int(cache_checked_ts)
            cache_updated_ts = int(cache_updated_ts)
        except (TypeError, ValueError):
            cache_checked_ts = None
            cache_updated_ts = None

        # if check timeout not expired, updating is not required
        if cache_checked_ts:
            if time.time() < (cache_checked_ts + self.check_timeout):
                return cache_updated_ts

        # if other process is already updating cache, we do nothing
        if not self.app.cache.add(self.cache_key_updating, 1, self.lock_timeout):
            return cache_updated_ts

        now_ts = int(time.time())
        db_updated_ts = self.get_updated_ts_from_db()

        # if db not changed, we update cache checked ts
        if cache_updated_ts and db_updated_ts <= cache_updated_ts:
            with self.app.cache.pipe() as pipe:
                pipe.set(self.cache_key_checked_ts, now_ts)
                pipe.delete(self.cache_key_updating)
                pipe.execute()
            return cache_updated_ts

        # Get sections from db
        sections_meta, sections_body = self.get_sections_from_db()

        sections_meta = {s_id: self._dumps(s) \
            for s_id, s in sections_meta.items()}
        sections_body = {s_id: self._dumps(s) \
            for s_id, s in sections_body.items()}

        # Update cache 
        with self.app.cache.pipe() as pipe:
            pipe.set(self.cache_key_updated_ts, db_updated_ts)
            pipe.set(self.cache_key_checked_ts, now_ts)
            pipe.delete(self.cache_key_updating)
            pipe.delete(self.cache_key_meta)
            pipe.delete(self.cache_key_body)
            pipe.hmset(self.cache_key_meta, sections_meta)
            if sections_body:
                pipe.hmset(self.cache_key_body, sections_body)
            pipe.execute()
        return db_updated_ts

    def get_updated_ts_from_db(self):
        session = self.app.db()
        row = self.query_cls(func.max(self.model.updated_dt), session).first()
        session.close()
        if row:
            return int(time.mktime(row[0].timetuple()))
        else:
            return None

    def get_sections_from_db(self):
        session = self.app.db()
        sections_objs = self.query_cls(self.model, session).\
            order_by(self.model.order).all()

        objs_by_id = {obj.id: obj for obj in sections_objs}
        sections = [section.to_meta_dict() \
            for section in sections_objs if section.public]
        sections_by_id = {section['id']: section for section in sections}
        sections_by_parent = {}
        for section in sections:
            if section['parent_id'] is not None:
                parent_section = sections_by_id.get(section['parent_id'])
                if not parent_section:
                    continue
            sections = sections_by_parent.setdefault(section['parent_id'], [])
            if section['slug'] not in [s['slug'] for s in sections]:
                sections.append(section)

        def walk_sections(sections_by_parent, parent_section=None):
            result = []
            if parent_section is None:
                parent_id = None
                parents = []
            else:
                parent_id = parent_section['id']
                parents = parent_section['parents'] + [parent_section['id']]
            for section in sections_by_parent.get(parent_id, []):
                used_slugs = set()
                if section['slug'] in used_slugs:
                    continue
                else:
                    used_slugs.add(section['slug'])
                section = section.copy()
                section['parents'] = list(parents)
                children = sections_by_parent.get(section['id'], [])
                section['children'] = [c['id'] for c in children]
                result.append(section)
                result += walk_sections(sections_by_parent, section)
            return result

        sections = walk_sections(sections_by_parent)
        sections_meta = {section['id']: section for section in sections}
        sections_body = {sid: objs_by_id[sid].to_body_dict()\
            for sid in sections_meta}
        root_sections_ids = [s['id'] for s in sections_by_parent.get(None, [])]
        sections_meta[''] = {'children': root_sections_ids}
        session.close()
        return sections_meta, sections_body

    def get_sections(self, ids):
        return self._get_sections_meta(ids)

    def get_section(self, id):
        return self.get_sections([id])[0]

    def get_sections_with_body(self, ids):
        with self.app.cache.pipe() as pipe:
            metas = self._get_sections_meta(ids)
            bodies = self._get_sections_bodies(ids)
        sections = []
        for meta, body in zip(metas, bodies):
            if meta is not None and body is not None:
                section = dict(meta, **body)
            else:
                section = None
            sections.append(section)
        return sections

    def get_section_with_body(self, id):
        return self.get_sections_with_body([id])[0]

    def h_subsections(self, section):
        handlers = []
        subsections = self.get_sections(section['children'])
        for subsection in subsections:
            if not subsection:
                continue
            view = self.views[subsection['type']]
            handler = view.handler(self, subsection)
            handlers.append(handler)
        return h_cases(*handlers)

    def h_sections(self):
        section = self.get_section('')
        return self.h_subsections(section)

    def _get_sections_meta(self, ids):
        if not ids:
            return []
        raw_sections = self.app.cache.client.hmget(self.cache_key_meta, ids)
        sections = []
        for id, section in zip(ids, raw_sections):
            if section is not None:
                section = self._loads(section)
            sections.append(section)
        return sections

    def _get_sections_bodies(self, ids):
        if not ids:
            return []
        raw_sections = self.app.cache.client.hmget(self.cache_key_body, ids)
        sections = []
        for id, section in zip(ids, raw_sections):
            if section is not None:
                section = self._loads(section)
            sections.append(section)
        return sections

    def _dumps(self, obj):
        return pickle.dumps(obj)

    def _loads(self, string):
        return pickle.loads(string)


component = Component.create_cls