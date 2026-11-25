import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo
from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.types import Message, Chat, User, Update, CallbackQuery, PhotoSize, InlineKeyboardMarkup
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from database import Database
from workspace_db import initialize, create_section, get_item, get_section, update_item
from handlers import get_routers
from handlers.ui import NavigationMiddleware
from handlers.navigation import ScreenNavigation, ScreenRequests, _anchors, KEYBOARD_CARRIER
from handlers.workspace import WS
from middlewares import AdminOnlyMiddleware

class FakeSession(BaseSession):
    def __init__(self):
        super().__init__(); self.messages = {}; self.deleted = []; self.next_id = 1000; self.calls = []; self.no_change = False
        self.fail_delete_once = False
        self.fail_edit_once = False
    async def close(self): pass
    async def stream_content(self, *args, **kwargs):
        yield b''
    async def make_request(self, bot, method, timeout=None):
        name = method.__api_method__
        self.calls.append(method)
        if name == 'answerCallbackQuery': return True
        if name == 'deleteMessage':
            if self.fail_delete_once:
                self.fail_delete_once = False
                raise TelegramNetworkError(method=method,message='Temporary connection error')
            self.deleted.append(method.message_id); self.messages.pop(method.message_id,None); return True
        if name.startswith('edit'):
            if self.fail_edit_once:
                self.fail_edit_once = False
                raise TelegramNetworkError(method=method,message='Temporary rendering error')
            if self.no_change and name in {'editMessageText', 'editMessageMedia', 'editMessageCaption'}:
                self.no_change = False
                raise TelegramBadRequest(method=method,message='message is not modified')
            old = self.messages.get(method.message_id)
            if old is None: raise TelegramBadRequest(method=method,message='message not found')
            update = {'reply_markup':getattr(method,'reply_markup',None)}
            if name == 'editMessageText': update['text'] = method.text
            if name == 'editMessageMedia': update.update(text=None,caption=method.media.caption,photo=[PhotoSize(file_id='photo',file_unique_id='photo',width=100,height=100)])
            result = old.model_copy(update=update)
        else:
            self.next_id += 1
            result = Message(message_id=self.next_id,date=1,chat=Chat(id=5,type='private'),from_user=User(id=123,is_bot=True,first_name='Bot'),text=getattr(method,'text',None),caption=getattr(method,'caption',None),photo=[PhotoSize(file_id='photo',file_unique_id='photo',width=100,height=100)] if name=='sendPhoto' else None,reply_markup=getattr(method,'reply_markup',None) if isinstance(getattr(method,'reply_markup',None), InlineKeyboardMarkup) else None)
        self.messages[result.message_id] = result
        return result

class Navigation(unittest.IsolatedAsyncioTestCase):
    async def test_menu_search_card_pages_and_deleted_chat(self):
        with tempfile.TemporaryDirectory() as folder:
            db = Database(Path(folder)/'test.db'); await db.connect(); await initialize(db)
            await db.conn.execute('CREATE TABLE ui_messages(chat INTEGER,message INTEGER,PRIMARY KEY(chat,message))'); await db.conn.commit()
            root = Path(folder)
            settings = SimpleNamespace(tz=ZoneInfo('Europe/Kaliningrad'),storage_dir=root/'storage',
                                       backup_dir=root/'backups',max_content_chars=1000000)
            settings.storage_dir.mkdir()
            session = FakeSession(); bot = Bot('123:FAKE',session=session); session.middleware(ScreenRequests(db))
            dp = Dispatcher(); dp['db']=db; dp['settings']=settings
            dp.message.outer_middleware(NavigationMiddleware()); dp.update.outer_middleware(AdminOnlyMiddleware(5)); dp.update.outer_middleware(ScreenNavigation(db))
            for router in get_routers(): dp.include_router(router)
            user = User(id=5,is_bot=False,first_name='Test'); number = 0
            async def send(text, *, visible=False):
                nonlocal number
                number += 1
                message = Message(message_id=number,date=1,chat=Chat(id=5,type='private'),from_user=user,text=text)
                if visible:
                    session.messages[number] = message
                await dp.feed_update(bot,Update(update_id=number,message=message))
            async def click(data, actor=None):
                nonlocal number
                number += 1
                visible = [m for m in session.messages.values() if m.reply_markup]
                await dp.feed_update(bot,Update(update_id=number,callback_query=CallbackQuery(id=str(number),from_user=actor or user,chat_instance='test',message=visible[-1],data=data)))
            try:
                await send('/start'); self.assertTrue(5 in _anchors)
                await send('🔎 Поиск')
                state = dp.fsm.get_context(bot=bot,chat_id=5,user_id=5)
                self.assertIsNotNone(await state.get_state())
                await send('🗂 База знаний'); self.assertIsNone(await state.get_state())
                item = await db.add_item(category='note',title='Long',content='Текст 😀 '*700)
                await click(WS(action='card',id=item).pack())
                card = [m for m in session.messages.values() if m.reply_markup][-1]
                self.assertIn('Далее',str(card.reply_markup))
                page = [b.callback_data for row in card.reply_markup.inline_keyboard for b in row if b.text=='Далее ›'][0]
                await click(page)
                from PIL import Image
                Image.new('RGB',(100,50),'white').save(settings.storage_dir/'photo.tiff')
                photo = await db.add_item(category='screen',title='Photo',content='Caption',file_path='photo.tiff')
                await click(WS(action='card',id=photo).pack())
                card = [m for m in session.messages.values() if m.reply_markup][-1]
                self.assertTrue(card.photo); self.assertIn('Caption',card.caption)
                await send('❓ Помощь')
                self.assertLessEqual(len(session.messages),2)
                session.messages.clear()
                await send('/menu'); self.assertTrue(session.messages)
                await send('🗂 База знаний'); self.assertLessEqual(len(session.messages),2)
                anchor = _anchors[5]
                self.assertEqual(session.messages[anchor].text, KEYBOARD_CARRIER)
                session.no_change = True
                await send('🗂 База знаний')
                self.assertIn(number, session.deleted)
                self.assertEqual(_anchors[5], anchor)
                self.assertFalse(any(m.text == 'Меню' for m in session.messages.values()))
                session.fail_delete_once = True
                await send('📅\ufe0f Дедлайны')
                self.assertIn(number, session.deleted)
                self.assertTrue(any('Дедлайны' in (m.caption or m.text or '') for m in session.messages.values()))
                session.fail_edit_once = True
                with self.assertRaises(TelegramNetworkError):
                    await send('🗂\ufe0f База знаний')
                self.assertIn(number, session.deleted)
                await send('🗂 База знаний')
                self.assertIn(number, session.deleted)


                timing_id = await db.create_timing("Рабочая задача")
                await db.start_timing(timing_id)
                await db.stop_timing(timing_id)
                for _ in range(2):
                    await send('📊 Отчёт', visible=True)
                    self.assertNotIn(number, session.messages)
                    reports = [m for m in session.messages.values() if 'Тайминги за' in (m.caption or '')]
                    self.assertEqual(len(reports), 1)
                    self.assertEqual(set(session.messages), {_anchors[5], reports[0].message_id})
                await send('🗂 База знаний', visible=True)
                self.assertNotIn(number, session.messages)
                self.assertFalse(any('Тайминги за' in (m.caption or '') for m in session.messages.values()))
                self.assertLessEqual(len(session.messages), 2)

                parent = await create_section(db, 'Проверка корня')
                child = await create_section(db, 'Проверка вложения', parent)
                await update_item(db, item, 'section_id', parent)
                await update_item(db, photo, 'section_id', child)
                def screen():
                    return [m for m in session.messages.values() if isinstance(m.reply_markup,InlineKeyboardMarkup)][-1]
                def press(action, section=0, item_id=0):
                    return click(WS(action=action,id=section,item=item_id).pack())
                await press('section', parent)
                self.assertIn('Long', str(screen().reply_markup))
                self.assertNotIn('📄 Photo', str(screen().reply_markup))
                self.assertTrue(any(getattr(getattr(call,'media',None),'media',None)
                                    and call.media.media.filename==f'section-{parent}.jpg'
                                    for call in session.calls))
                await press('section', child)
                self.assertIn('📄 Photo', str(screen().reply_markup))
                self.assertNotIn('📄 Long', str(screen().reply_markup))
                await press('secmanage', child)
                await press('secrename', child)
                await send('Фото и файлы')
                self.assertEqual((await get_section(db, child))['name'], 'Фото и файлы')
                self.assertIn('Фото и файлы', screen().caption)

                await press('secdelask', child)
                self.assertIn('Записи переместятся',screen().caption)
                self.assertIsNotNone(await get_section(db, child))
                stranger = User(id=6,is_bot=False,first_name='Stranger')
                await click(WS(action='secdel',id=child).pack(),actor=stranger)
                self.assertIsNotNone(await get_section(db, child))
                await press('secdel', child)
                self.assertIsNone(await get_section(db, child))
                self.assertEqual((await get_item(db,photo))['section_id'],parent)
                self.assertTrue((settings.storage_dir/'photo.tiff').is_file())
                self.assertIn('📄 Photo',str(screen().reply_markup))

                await press('secdelask', parent)
                await press('secdel', parent)
                await press('unfiled')
                self.assertIn('📄 Photo',str(screen().reply_markup))
                self.assertIn('📄 Long',str(screen().reply_markup))
                self.assertIsNone((await get_item(db,photo))['section_id'])

                documents_before = sum(call.__api_method__ == 'sendDocument' for call in session.calls)
                await send('🗄 Резервная копия')
                self.assertEqual(len(list(settings.backup_dir.glob('*.zip'))),1)
                self.assertEqual(sum(call.__api_method__ == 'sendDocument' for call in session.calls), documents_before)
                self.assertFalse(any(m.text=='Меню' for m in session.messages.values()))

            finally:
                await db.close(); await bot.session.close()
