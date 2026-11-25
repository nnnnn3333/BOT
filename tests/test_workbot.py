import tempfile
import unittest
import zipfile
import sqlite3
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo
from html.parser import HTMLParser
from database import Database
from workspace_db import initialize, get_item, set_favorite, list_items, remember_browse, browse_context, add_deadline, reschedule_deadline
from handlers.text_pages import split_html
from handlers.navigation import Screen, _current, mark_saved_input, preserve_messages
from backup import _write_zip

class Plain(HTMLParser):
    def __init__(self):
        super().__init__(); self.text = ''; self.stack = []
    def handle_starttag(self, tag, attrs): self.stack.append(tag)
    def handle_endtag(self, tag):
        assert self.stack.pop() == tag
    def handle_data(self, text): self.text += text

class Pages(unittest.TestCase):
    def test_unicode_and_html_are_preserved(self):
        original = ('Текст & < > 😀 ' * 500)
        import html
        pages = split_html('<b>Заголовок</b><i>'+html.escape(original)+'</i>', 900)
        result = ''
        for page in pages:
            parser = Plain(); parser.feed(page)
            self.assertEqual(parser.stack, [])
            self.assertLessEqual(len(parser.text.encode('utf-16-le'))//2, 900)
            result += parser.text
        self.assertEqual(result, 'Заголовок'+original)
    def test_preserving_exports_restores_screen(self):
        screen = Screen(1); token = _current.set(screen)
        try:
            with preserve_messages():
                self.assertIsNone(_current.get()); mark_saved_input()
            self.assertFalse(screen.saved_input)
            mark_saved_input(); self.assertTrue(screen.saved_input)
        finally: _current.reset(token)

class Storage(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name)
        self.db = Database(self.root/'live.db'); await self.db.connect()
        self.id = await self.db.add_item(category='note',title='Старая запись',content='Материал')
        await initialize(self.db)
    async def asyncTearDown(self):
        await self.db.close(); self.tmp.cleanup()
    async def test_upgrade_and_favorites(self):
        await initialize(self.db)
        self.assertEqual((await get_item(self.db,self.id))['content'], 'Материал')
        await set_favorite(self.db,self.id,True); await set_favorite(self.db,self.id,True)
        rows, more = await list_items(self.db,filter_name='favorite')
        self.assertEqual([r['id'] for r in rows],[self.id])
        await set_favorite(self.db,self.id,False)
        self.assertEqual(len((await list_items(self.db,filter_name='favorite'))[0]),0)
    async def test_browse_survives_reconnect(self):
        await remember_browse(self.db,7,'search',page=2,query='Материал')
        await self.db.close(); await self.db.connect()
        row = await browse_context(self.db,7)
        self.assertEqual((row['page'],row['query']),(2,'Материал'))
    async def test_deadline_updates_card(self):
        when = datetime(2026,11,22,12,tzinfo=ZoneInfo('Europe/Kaliningrad'))
        deadline = await add_deadline(self.db,'Срок',when,self.id)
        moved = datetime(2026,11,25,0,30,tzinfo=when.tzinfo)
        self.assertTrue(await reschedule_deadline(self.db,deadline,moved))
        self.assertEqual((await get_item(self.db,self.id))['valid_until'],'2026-11-25')
        self.assertFalse(await reschedule_deadline(self.db,999,moved))
    async def test_three_days_boundary(self):
        for day in range(22,26):
            item = await self.db.add_item(category='note',title=str(day),content='')
            await self.db.conn.execute('INSERT INTO work_item_meta(item_id,valid_until) VALUES (?,?)',(item,f'2026-11-{day}'))
        await self.db.conn.commit()
        rows, more = await list_items(self.db,filter_name='soon',today='2026-11-22')
        self.assertEqual({r['title'] for r in rows},{'22','23','24'})
    async def test_ocr_does_not_overwrite_manual_edit(self):
        from unittest.mock import AsyncMock, patch
        from types import SimpleNamespace
        from handlers.ingest import _recognize_saved
        from workspace_db import update_item
        await update_item(self.db,self.id,'content','Правка пользователя')
        message = SimpleNamespace(answer=AsyncMock())
        with patch('handlers.ingest.recognize',AsyncMock(return_value=('OCR text','Готово'))):
            await _recognize_saved(message,self.db,SimpleNamespace(max_content_chars=10000),self.root/'photo',self.id,'Старая запись','Материал')
        self.assertEqual((await get_item(self.db,self.id))['content'],'Правка пользователя')

    async def test_backup_restores_database_and_original(self):
        storage = self.root/'storage'; storage.mkdir(); (storage/'original.txt').write_text('Original',encoding='utf-8')
        snapshot = self.root/'snapshot.db'; await self.db.snapshot(snapshot)
        archive = self.root/'backup.zip'; _write_zip(archive,snapshot,storage)
        restored = self.root/'restored'
        with zipfile.ZipFile(archive) as z:
            self.assertIsNone(z.testzip()); z.extractall(restored)
        from contextlib import closing
        with closing(sqlite3.connect(restored/'work_base.db')) as conn:
            self.assertEqual(conn.execute('PRAGMA quick_check').fetchone()[0],'ok')
            self.assertEqual(conn.execute('SELECT content FROM items WHERE id=?',(self.id,)).fetchone()[0],'Материал')
        self.assertEqual((restored/'storage/original.txt').read_text(encoding='utf-8'),'Original')

    async def test_root_delete_preserves_child_and_archive_restores_files(self):
        from types import SimpleNamespace
        from contextlib import closing
        from backup import save_backup
        from workspace_db import create_section, delete_section, get_section, update_item

        parent = await create_section(self.db, 'Папка')
        child = await create_section(self.db, 'Подпапка', parent)
        storage = self.root / 'storage'
        storage.mkdir()
        (storage / 'file.txt').write_bytes(b'attachment')
        item = await self.db.add_item(category='file',title='Файл',file_path='file.txt')
        await update_item(self.db,item,'section_id',child)
        self.assertEqual(await delete_section(self.db,parent),(True,None))
        self.assertIsNone((await get_section(self.db,child))['parent_id'])
        self.assertEqual((await get_item(self.db,item))['section_id'],child)
        settings = SimpleNamespace(tz=ZoneInfo('Europe/Kaliningrad'),storage_dir=storage,
                                   backup_dir=self.root/'backups')
        archive = await save_backup(self.db,settings)
        self.assertEqual(archive.parent,settings.backup_dir)
        restored = self.root/'restored.db'
        with zipfile.ZipFile(archive) as contents:
            restored.write_bytes(contents.read('work_base.db'))
            self.assertEqual(contents.read('storage/file.txt'),b'attachment')
        with closing(sqlite3.connect(restored)) as conn:
            self.assertEqual(conn.execute('PRAGMA quick_check').fetchone()[0],'ok')
            self.assertEqual(conn.execute('SELECT section_id FROM work_item_meta WHERE item_id=?',(item,)).fetchone(),(child,))

    async def test_deleting_all_sections_does_not_recreate_defaults(self):
        from workspace_db import list_sections, delete_section
        while sections := await list_sections(self.db):
            for section in sections:
                await delete_section(self.db,section['id'])
        await initialize(self.db)
        self.assertEqual(await list_sections(self.db),[])


if __name__ == '__main__': unittest.main()
