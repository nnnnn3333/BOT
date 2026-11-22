import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo
from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.types import Message, Chat, User, Update, CallbackQuery, PhotoSize
from aiogram.exceptions import TelegramBadRequest
from database import Database
from workspace_db import initialize
from handlers import get_routers
from handlers.ui import NavigationMiddleware
from handlers.navigation import ScreenNavigation, ScreenRequests, _anchors
from handlers.workspace import WS
from middlewares import AdminOnlyMiddleware

class FakeSession(BaseSession):
    def __init__(self):
        super().__init__(); self.messages = {}; self.deleted = []; self.next_id = 1000
    async def close(self): pass
    async def stream_content(self, *args, **kwargs):
        yield b''
    async def make_request(self, bot, method, timeout=None):
        name = method.__api_method__
        if name == 'answerCallbackQuery': return True
        if name == 'deleteMessage':
            self.deleted.append(method.message_id); self.messages.pop(method.message_id,None); return True
        if name.startswith('edit'):
            old = self.messages.get(method.message_id)
            if old is None: raise TelegramBadRequest(method=method,message='message not found')
            update = {'reply_markup':getattr(method,'reply_markup',None)}
            if name == 'editMessageText': update['text'] = method.text
            if name == 'editMessageMedia': update.update(text=None,caption=method.media.caption,photo=[PhotoSize(file_id='photo',file_unique_id='photo',width=100,height=100)])
            result = old.model_copy(update=update)
        else:
            self.next_id += 1
            result = Message(message_id=self.next_id,date=1,chat=Chat(id=5,type='private'),from_user=User(id=123,is_bot=True,first_name='Bot'),text=getattr(method,'text',None),caption=getattr(method,'caption',None),photo=[PhotoSize(file_id='photo',file_unique_id='photo',width=100,height=100)] if name=='sendPhoto' else None,reply_markup=getattr(method,'reply_markup',None) if name not in ('sendMessage',) else None)
        self.messages[result.message_id] = result
        return result

class Navigation(unittest.IsolatedAsyncioTestCase):
    async def test_menu_search_card_pages_and_deleted_chat(self):
        with tempfile.TemporaryDirectory() as folder:
            db = Database(Path(folder)/'test.db'); await db.connect(); await initialize(db)
            await db.conn.execute('CREATE TABLE ui_messages(chat INTEGER,message INTEGER,PRIMARY KEY(chat,message))'); await db.conn.commit()
            settings = SimpleNamespace(tz=ZoneInfo('Europe/Kaliningrad'),storage_dir=Path(folder),max_content_chars=1000000)
            session = FakeSession(); bot = Bot('123:FAKE',session=session); session.middleware(ScreenRequests(db))
            dp = Dispatcher(); dp['db']=db; dp['settings']=settings
            dp.message.outer_middleware(NavigationMiddleware()); dp.update.outer_middleware(AdminOnlyMiddleware(5)); dp.update.outer_middleware(ScreenNavigation(db))
            for router in get_routers(): dp.include_router(router)
            user = User(id=5,is_bot=False,first_name='Test'); number = 0
            async def send(text):
                nonlocal number
                number += 1
                await dp.feed_update(bot,Update(update_id=number,message=Message(message_id=number,date=1,chat=Chat(id=5,type='private'),from_user=user,text=text)))
            async def click(data):
                nonlocal number
                number += 1
                visible = [m for m in session.messages.values() if m.reply_markup]
                await dp.feed_update(bot,Update(update_id=number,callback_query=CallbackQuery(id=str(number),from_user=user,chat_instance='test',message=visible[-1],data=data)))
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
                Image.new('RGB',(100,50),'white').save(Path(folder)/'photo.tiff')
                photo = await db.add_item(category='screen',title='Photo',content='Caption',file_path='photo.tiff')
                await click(WS(action='card',id=photo).pack())
                card = [m for m in session.messages.values() if m.reply_markup][-1]
                self.assertTrue(card.photo); self.assertIn('Caption',card.caption)
                await send('❓ Помощь')
                self.assertLessEqual(len(session.messages),2)
                session.messages.clear()
                await send('/menu'); self.assertTrue(session.messages)
                await send('🗂 База знаний'); self.assertLessEqual(len(session.messages),2)
            finally:
                await db.close(); await bot.session.close()
