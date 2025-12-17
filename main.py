import discord
from discord import app_commands
from discord.ext import commands
import asyncpg
import os
import random
import time
from typing import Optional, List, Tuple, Dict
from datetime import datetime, timedelta
import asyncio

# Конфигурация
DATABASE_URL = os.getenv('DATABASE_URL')  # Session pooler connection string
TOKEN = os.getenv('DISCORD_TOKEN')

intents = discord.Intents.default()
intents.message_content = True

class MinesweeperBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix='!', intents=intents)
        self.db_pool = None
        self.active_games = {}  # Хранение игр в памяти для скорости
    
    async def setup_hook(self):
        await self.tree.sync()
        self.db_pool = await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=10)
        await self.init_database()
    
    async def init_database(self):
        async with self.db_pool.acquire() as conn:
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS players (
                    user_id BIGINT PRIMARY KEY,
                    username TEXT,
                    total_blocks_cleared INTEGER DEFAULT 0,
                    total_time_spent FLOAT DEFAULT 0,
                    best_speed FLOAT DEFAULT 0,
                    games_played INTEGER DEFAULT 0,
                    best_blocks_normal INTEGER DEFAULT 0,
                    best_blocks_hardcore INTEGER DEFAULT 0,
                    created_at TIMESTAMP DEFAULT NOW()
                )
            ''')
            
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS speed_leaderboard (
                    user_id BIGINT PRIMARY KEY,
                    username TEXT,
                    avg_speed FLOAT,
                    total_blocks INTEGER,
                    total_time FLOAT,
                    last_updated TIMESTAMP DEFAULT NOW(),
                    FOREIGN KEY (user_id) REFERENCES players(user_id)
                )
            ''')
            
            await conn.execute('CREATE INDEX IF NOT EXISTS idx_players_best_speed ON players(best_speed DESC)')
            await conn.execute('CREATE INDEX IF NOT EXISTS idx_speed_leaderboard ON speed_leaderboard(avg_speed DESC)')

bot = MinesweeperBot()

class MinesweeperGame:
    def __init__(self, mode='normal', is_multiplayer=False):
        self.mode = mode
        self.is_multiplayer = is_multiplayer
        self.blocks_cleared = 0
        self.start_time = time.time()
        self.last_action_time = time.time()
        self.hardcore_timer = 30.0 if mode == 'hardcore' else 0
        
        # Вертикальная цепочка блоков: {block_index: {grid, mines, cells_revealed, message_id, completed}}
        self.blocks: Dict[int, dict] = {}
        self.current_max_block = 1  # Максимальный отправленный блок
        
        # Генерируем первые 2 блока
        self.generate_block(0)
        self.generate_block(1)
    
    def generate_block(self, block_index: int):
        """Генерирует один блок 5x5"""
        if self.mode == 'hardcore':
            base_mines = 5
            mines_count = min(base_mines + (self.blocks_cleared // 3), 12)
        else:
            mines_count = 5
        
        grid = [[0 for _ in range(5)] for _ in range(5)]
        mines = set()
        
        while len(mines) < mines_count:
            x, y = random.randint(0, 4), random.randint(0, 4)
            if (x, y) not in mines:
                mines.add((x, y))
                grid[y][x] = -1
        
        # Вычисляем числа
        for y in range(5):
            for x in range(5):
                if grid[y][x] != -1:
                    count = 0
                    for dy in [-1, 0, 1]:
                        for dx in [-1, 0, 1]:
                            ny, nx = y + dy, x + dx
                            if 0 <= ny < 5 and 0 <= nx < 5 and grid[ny][nx] == -1:
                                count += 1
                    grid[y][x] = count
        
        self.blocks[block_index] = {
            'grid': grid,
            'mines': mines,
            'cells_revealed': set(),
            'message_id': None,
            'completed': False
        }
    
    def get_time_bonus_hardcore(self):
        base_bonus = 18
        reduction = (self.blocks_cleared // 5) * 1
        return max(5, base_bonus - reduction)
    
    def reveal_cell(self, block_idx: int, x: int, y: int):
        """Открывает клетку"""
        if block_idx not in self.blocks:
            return 'invalid', set()
        
        block = self.blocks[block_idx]
        if block['completed']:
            return 'invalid', set()
        
        grid = block['grid']
        
        if (x, y) in block['cells_revealed']:
            return 'already_revealed', set()
        
        if grid[y][x] == -1:
            return 'mine', {(x, y)}
        
        # Flood fill
        revealed = set()
        stack = [(x, y)]
        
        while stack:
            cx, cy = stack.pop()
            if (cx, cy) in block['cells_revealed'] or (cx, cy) in revealed:
                continue
            
            revealed.add((cx, cy))
            
            if grid[cy][cx] == 0:
                for dy in [-1, 0, 1]:
                    for dx in [-1, 0, 1]:
                        nx, ny = cx + dx, cy + dy
                        if 0 <= nx < 5 and 0 <= ny < 5:
                            if (nx, ny) not in block['cells_revealed']:
                                stack.append((nx, ny))
        
        return 'safe', revealed
    
    def is_block_complete(self, block_idx: int) -> bool:
        """Проверяет, пройден ли блок"""
        if block_idx not in self.blocks:
            return False
        
        block = self.blocks[block_idx]
        total_safe = 25 - len(block['mines'])
        revealed_safe = len(block['cells_revealed'])
        
        return revealed_safe == total_safe

class MinesweeperView(discord.ui.View):
    def __init__(self, game: MinesweeperGame, block_idx: int, user_id: int, thread_id: int):
        super().__init__(timeout=None)
        self.game = game
        self.block_idx = block_idx
        self.user_id = user_id
        self.thread_id = thread_id
        self.update_buttons()
    
    def update_buttons(self):
        self.clear_items()
        
        if self.block_idx not in self.game.blocks:
            return
        
        block = self.game.blocks[self.block_idx]
        grid = block['grid']
        
        for y in range(5):
            for x in range(5):
                button = MinesweeperButton(x, y, self.block_idx)
                
                if (x, y) in block['cells_revealed']:
                    button.disabled = True
                    value = grid[y][x]
                    if value == 0:
                        button.label = '·'
                        button.style = discord.ButtonStyle.secondary
                    else:
                        button.label = str(value)
                        button.style = discord.ButtonStyle.primary
                
                self.add_item(button)
    
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not self.game.is_multiplayer and interaction.user.id != self.user_id:
            await interaction.response.send_message("Это не ваша игра!", ephemeral=True)
            return False
        return True

class MinesweeperButton(discord.ui.Button):
    def __init__(self, x: int, y: int, block_idx: int):
        super().__init__(style=discord.ButtonStyle.success, label='❔', row=y)
        self.x = x
        self.y = y
        self.block_idx = block_idx
    
    async def callback(self, interaction: discord.Interaction):
        view: MinesweeperView = self.view
        game = view.game
        
        # ОПТИМИЗАЦИЯ: Немедленный defer для скорости
        await interaction.response.defer()
        
        game.last_action_time = time.time()
        
        result, revealed = game.reveal_cell(self.block_idx, self.x, self.y)
        
        if result == 'invalid' or result == 'already_revealed':
            return
        
        if result == 'mine':
            await self.handle_game_over(interaction, view)
            return
        
        # Добавляем открытые клетки
        block = game.blocks[self.block_idx]
        block['cells_revealed'].update(revealed)
        
        # Проверяем, завершён ли блок
        if game.is_block_complete(self.block_idx):
            await self.handle_block_complete(interaction, view)
        else:
            # ОПТИМИЗАЦИЯ: Обновляем только кнопки, без пересоздания view
            view.update_buttons()
            
            timer_text = ""
            if game.mode == 'hardcore':
                timer_text = f" | ⏱️ {game.hardcore_timer:.1f}с"
            
            try:
                await interaction.edit_original_response(
                    content=f"🎮 Блок #{self.block_idx + 1}{timer_text}",
                    view=view
                )
            except:
                pass
    
    async def handle_game_over(self, interaction: discord.Interaction, view: MinesweeperView):
        game = view.game
        
        # Показываем все бомбы в текущем блоке
        block = game.blocks[self.block_idx]
        for x, y in block['mines']:
            block['cells_revealed'].add((x, y))
        
        view.update_buttons()
        for item in view.children:
            item.disabled = True
        
        total_time = time.time() - game.start_time
        avg_speed = game.blocks_cleared / total_time if total_time > 0 and game.blocks_cleared > 0 else 0
        
        # Сохраняем статистику
        async with bot.db_pool.acquire() as conn:
            if game.mode == 'hardcore':
                await conn.execute('''
                    INSERT INTO players (user_id, username, total_blocks_cleared, total_time_spent, best_speed, games_played, best_blocks_hardcore)
                    VALUES ($1, $2, $3, $4, $5, 1, $6)
                    ON CONFLICT (user_id) DO UPDATE SET
                        total_blocks_cleared = players.total_blocks_cleared + $3,
                        total_time_spent = players.total_time_spent + $4,
                        best_speed = CASE WHEN $5 > players.best_speed THEN $5 ELSE players.best_speed END,
                        games_played = players.games_played + 1,
                        best_blocks_hardcore = CASE WHEN $6 > players.best_blocks_hardcore THEN $6 ELSE players.best_blocks_hardcore END
                ''', interaction.user.id, str(interaction.user), game.blocks_cleared, total_time, avg_speed, game.blocks_cleared)
            else:
                await conn.execute('''
                    INSERT INTO players (user_id, username, total_blocks_cleared, total_time_spent, best_speed, games_played, best_blocks_normal)
                    VALUES ($1, $2, $3, $4, $5, 1, $6)
                    ON CONFLICT (user_id) DO UPDATE SET
                        total_blocks_cleared = players.total_blocks_cleared + $3,
                        total_time_spent = players.total_time_spent + $4,
                        best_speed = CASE WHEN $5 > players.best_speed THEN $5 ELSE players.best_speed END,
                        games_played = players.games_played + 1,
                        best_blocks_normal = CASE WHEN $6 > players.best_blocks_normal THEN $6 ELSE players.best_blocks_normal END
                ''', interaction.user.id, str(interaction.user), game.blocks_cleared, total_time, avg_speed, game.blocks_cleared)
            
            # Обновляем speed leaderboard
            total_blocks = await conn.fetchval('SELECT total_blocks_cleared FROM players WHERE user_id = $1', interaction.user.id)
            total_time_all = await conn.fetchval('SELECT total_time_spent FROM players WHERE user_id = $1', interaction.user.id)
            new_avg_speed = total_blocks / total_time_all if total_time_all > 0 else 0
            
            await conn.execute('''
                INSERT INTO speed_leaderboard (user_id, username, avg_speed, total_blocks, total_time)
                VALUES ($1, $2, $3, $4, $5)
                ON CONFLICT (user_id) DO UPDATE SET
                    avg_speed = $3, total_blocks = $4, total_time = $5, last_updated = NOW()
            ''', interaction.user.id, str(interaction.user), new_avg_speed, total_blocks, total_time_all)
        
        # Удаляем игру из памяти
        if view.thread_id in bot.active_games:
            del bot.active_games[view.thread_id]
        
        mode_emoji = "💀" if game.mode == "hardcore" else "💣"
        try:
            await interaction.edit_original_response(
                content=f"{mode_emoji} **ИГРА ОКОНЧЕНА!**\n"
                        f"Блоков пройдено: **{game.blocks_cleared}**\n"
                        f"Время игры: **{total_time:.2f}с**\n"
                        f"Средняя скорость: **{avg_speed:.3f} блоков/сек**",
                view=view
            )
        except:
            pass
    
    async def handle_block_complete(self, interaction: discord.Interaction, view: MinesweeperView):
        game = view.game
        thread = interaction.channel
        
        # Помечаем блок как завершённый
        game.blocks[self.block_idx]['completed'] = True
        game.blocks_cleared += 1
        
        # Обновляем хардкор таймер
        if game.mode == 'hardcore':
            bonus = game.get_time_bonus_hardcore()
            game.hardcore_timer += bonus
        
        # Отключаем кнопки завершённого блока
        for item in view.children:
            item.disabled = True
        
        try:
            await interaction.edit_original_response(
                content=f"✅ **Блок #{self.block_idx + 1} пройден!**",
                view=view
            )
        except:
            pass
        
        # Проверяем, все ли видимые блоки завершены
        all_visible_complete = all(
            game.blocks[i]['completed'] 
            for i in range(game.current_max_block + 1) 
            if i in game.blocks
        )
        
        if all_visible_complete:
            # Удаляем завершённые блоки
            completed_blocks = [i for i in game.blocks if game.blocks[i]['completed']]
            for block_idx in completed_blocks:
                try:
                    msg_id = game.blocks[block_idx]['message_id']
                    if msg_id:
                        msg = await thread.fetch_message(msg_id)
                        await msg.delete()
                except:
                    pass
                del game.blocks[block_idx]
            
            # Генерируем новые блоки
            new_block_1 = game.current_max_block + 1
            new_block_2 = game.current_max_block + 2
            
            game.generate_block(new_block_1)
            game.generate_block(new_block_2)
            game.current_max_block = new_block_2
            
            # Отправляем новые блоки
            await send_block(thread, game, new_block_1, view.user_id, view.thread_id)
            await send_block(thread, game, new_block_2, view.user_id, view.thread_id)

async def send_block(thread, game: MinesweeperGame, block_idx: int, user_id: int, thread_id: int):
    """Отправляет один блок 5x5"""
    timer_text = ""
    if game.mode == 'hardcore':
        timer_text = f" | ⏱️ {game.hardcore_timer:.1f}с"
    
    view = MinesweeperView(game, block_idx, user_id, thread_id)
    msg = await thread.send(
        f"🎮 **Блок #{block_idx + 1}**{timer_text}",
        view=view
    )
    
    game.blocks[block_idx]['message_id'] = msg.id

@bot.tree.command(name="minesweeper", description="Начать игру в бесконечный сапёр")
@app_commands.describe(
    mode="Режим игры",
    multiplayer="Игра для всех в канале"
)
@app_commands.choices(mode=[
    app_commands.Choice(name="🎮 Обычный", value="normal"),
    app_commands.Choice(name="💀 Хардкор", value="hardcore")
])
async def minesweeper(interaction: discord.Interaction, mode: str = "normal", multiplayer: bool = False):
    await interaction.response.defer()
    
    async with bot.db_pool.acquire() as conn:
        await conn.execute('''
            INSERT INTO players (user_id, username)
            VALUES ($1, $2)
            ON CONFLICT (user_id) DO NOTHING
        ''', interaction.user.id, str(interaction.user))
    
    mode_name = "💀 Хардкор" if mode == "hardcore" else "🎮 Обычный"
    mp_text = "👥 Мультиплеер" if multiplayer else f"👤 {interaction.user.display_name}"
    
    thread = await interaction.channel.create_thread(
        name=f"Сапёр: {mode_name} | {mp_text}",
        auto_archive_duration=60
    )
    
    game = MinesweeperGame(mode=mode, is_multiplayer=multiplayer)
    bot.active_games[thread.id] = game
    
    welcome_text = f"🎮 **Бесконечный Сапёр - {mode_name}**\n\n"
    if mode == "hardcore":
        welcome_text += "⏱️ Ограниченное время на прохождение!\n"
        welcome_text += "✅ Бонусное время за каждый блок\n"
        welcome_text += "⚡ Прогрессивная сложность!\n\n"
    else:
        welcome_text += "✨ Откройте все безопасные клетки\n"
        welcome_text += "🎯 Фиксированная сложность\n\n"
    
    if multiplayer:
        welcome_text += "👥 Все могут играть!\n"
    
    welcome_text += "\n📊 **Механика:**\n"
    welcome_text += "• Блоки выстроены вертикально\n"
    welcome_text += "• Пройдите блок → он исчезнет\n"
    welcome_text += "• Новый блок появится снизу\n"
    welcome_text += "• Бесконечное поле вниз! ⬇️\n\nУдачи! 🍀"
    
    await thread.send(welcome_text)
    
    # Отправляем первые 2 блока
    await send_block(thread, game, 0, interaction.user.id, thread.id)
    await send_block(thread, game, 1, interaction.user.id, thread.id)
    
    if mode == "hardcore":
        bot.loop.create_task(hardcore_timer_loop(thread.id, game, interaction.user.id))
    
    await interaction.followup.send(f"✅ Игра создана! {thread.mention}")

async def hardcore_timer_loop(thread_id: int, game: MinesweeperGame, user_id: int):
    """Таймер для хардкора"""
    while game.hardcore_timer > 0:
        await asyncio.sleep(0.5)
        game.hardcore_timer -= 0.5
        
        if game.hardcore_timer <= 0:
            try:
                thread = bot.get_channel(thread_id)
                if thread:
                    total_time = time.time() - game.start_time
                    avg_speed = game.blocks_cleared / total_time if total_time > 0 and game.blocks_cleared > 0 else 0
                    
                    async with bot.db_pool.acquire() as conn:
                        await conn.execute('''
                            INSERT INTO players (user_id, username, total_blocks_cleared, total_time_spent, best_speed, games_played, best_blocks_hardcore)
                            VALUES ($1, '', $2, $3, $4, 1, $5)
                            ON CONFLICT (user_id) DO UPDATE SET
                                total_blocks_cleared = players.total_blocks_cleared + $2,
                                total_time_spent = players.total_time_spent + $3,
                                best_speed = CASE WHEN $4 > players.best_speed THEN $4 ELSE players.best_speed END,
                                games_played = players.games_played + 1,
                                best_blocks_hardcore = CASE WHEN $5 > players.best_blocks_hardcore THEN $5 ELSE players.best_blocks_hardcore END
                        ''', user_id, game.blocks_cleared, total_time, avg_speed, game.blocks_cleared)
                        
                        total_blocks = await conn.fetchval('SELECT total_blocks_cleared FROM players WHERE user_id = $1', user_id)
                        total_time_all = await conn.fetchval('SELECT total_time_spent FROM players WHERE user_id = $1', user_id)
                        new_avg_speed = total_blocks / total_time_all if total_time_all > 0 else 0
                        
                        await conn.execute('''
                            INSERT INTO speed_leaderboard (user_id, username, avg_speed, total_blocks, total_time)
                            VALUES ($1, '', $2, $3, $4)
                            ON CONFLICT (user_id) DO UPDATE SET
                                avg_speed = $2, total_blocks = $3, total_time = $4, last_updated = NOW()
                        ''', user_id, new_avg_speed, total_blocks, total_time_all)
                    
                    if thread_id in bot.active_games:
                        del bot.active_games[thread_id]
                    
                    await thread.send(
                        f"⏰ **ВРЕМЯ ВЫШЛО!**\n"
                        f"Блоков пройдено: **{game.blocks_cleared}**\n"
                        f"Время игры: **{total_time:.2f}с**\n"
                        f"Средняя скорость: **{avg_speed:.3f} блоков/сек**"
                    )
            except:
                pass
            break

@bot.tree.command(name="leaderboard", description="Таблица лидеров")
async def leaderboard(interaction: discord.Interaction):
    await interaction.response.defer()
    
    async with bot.db_pool.acquire() as conn:
        records = await conn.fetch('''
            SELECT username, best_speed, total_blocks_cleared, games_played
            FROM players WHERE best_speed > 0
            ORDER BY best_speed DESC LIMIT 10
        ''')
        
        if not records:
            await interaction.followup.send("🏆 Таблица лидеров пуста!")
            return
        
        embed = discord.Embed(
            title="🏆 Таблица Лидеров",
            description="**Лучшая Скорость** - лучший результат за одну игру",
            color=discord.Color.gold()
        )
        
        medals = ["🥇", "🥈", "🥉"]
        leaderboard_text = ""
        
        for i, record in enumerate(records):
            medal = medals[i] if i < 3 else f"`{i+1}.`"
            leaderboard_text += f"{medal} **{record['username']}**\n"
            leaderboard_text += f"    ⚡ **{record['best_speed']:.3f}** блоков/сек\n"
            leaderboard_text += f"    📊 Игр: {record['games_played']} | Блоков: {record['total_blocks_cleared']}\n\n"
        
        embed.description += f"\n\n{leaderboard_text}"
    
    view = LeaderboardView("best")
    await interaction.followup.send(embed=embed, view=view)

class LeaderboardView(discord.ui.View):
    def __init__(self, current_type: str):
        super().__init__(timeout=120)
        self.current_type = current_type
        self.update_button()
    
    def update_button(self):
        self.clear_items()
        
        if self.current_type == "best":
            button = discord.ui.Button(
                label="⚡ Показать среднюю скорость",
                style=discord.ButtonStyle.primary,
                emoji="📊"
            )
            button.callback = self.show_average
        else:
            button = discord.ui.Button(
                label="🏆 Показать лучшую скорость",
                style=discord.ButtonStyle.success,
                emoji="🎯"
            )
            button.callback = self.show_best
        
        self.add_item(button)
    
    async def show_average(self, interaction: discord.Interaction):
        await interaction.response.defer()
        
        async with bot.db_pool.acquire() as conn:
            records = await conn.fetch('''
                SELECT username, avg_speed, total_blocks, total_time
                FROM speed_leaderboard WHERE avg_speed > 0
                ORDER BY avg_speed DESC LIMIT 10
            ''')
            
            if not records:
                await interaction.followup.send("⚡ Таблица пуста!", ephemeral=True)
                return
            
            embed = discord.Embed(
                title="🏆 Таблица Лидеров",
                description="**Средняя Скорость** - общий коэффициент (блоки ÷ время)",
                color=discord.Color.blue()
            )
            
            medals = ["🥇", "🥈", "🥉"]
            leaderboard_text = ""
            
            for i, record in enumerate(records):
                medal = medals[i] if i < 3 else f"`{i+1}.`"
                hours = int(record['total_time'] // 3600)
                minutes = int((record['total_time'] % 3600) // 60)
                time_str = f"{hours}ч {minutes}м" if hours > 0 else f"{minutes}м"
                
                leaderboard_text += f"{medal} **{record['username']}**\n"
                leaderboard_text += f"    ⚡ **{record['avg_speed']:.3f}** блоков/сек\n"
                leaderboard_text += f"    📊 Блоков: {record['total_blocks']} за {time_str}\n\n"
            
            embed.description += f"\n\n{leaderboard_text}"
        
        self.current_type = "average"
        self.update_button()
        await interaction.edit_original_response(embed=embed, view=self)
    
    async def show_best(self, interaction: discord.Interaction):
        await interaction.response.defer()
        
        async with bot.db_pool.acquire() as conn:
            records = await conn.fetch('''
                SELECT username, best_speed, total_blocks_cleared, games_played
                FROM players WHERE best_speed > 0
                ORDER BY best_speed DESC LIMIT 10
            ''')
            
            embed = discord.Embed(
                title="🏆 Таблица Лидеров",
                description="**Лучшая Скорость** - лучший результат за одну игру",
                color=discord.Color.gold()
            )
            
            medals = ["🥇", "🥈", "🥉"]
            leaderboard_text = ""
            
            for i, record in enumerate(records):
                medal = medals[i] if i < 3 else f"`{i+1}.`"
                leaderboard_text += f"{medal} **{record['username']}**\n"
                leaderboard_text += f"    ⚡ **{record['best_speed']:.3f}** блоков/сек\n"
                leaderboard_text += f"    📊 Игр: {record['games_played']} | Блоков: {record['total_blocks_cleared']}\n\n"
            
            embed.description += f"\n\n{leaderboard_text}"
        
        self.current_type = "best"
        self.update_button()
        await interaction.edit_original_response(embed=embed, view=self)

@bot.tree.command(name="profile", description="Профиль игрока")
async def profile(interaction: discord.Interaction, user: discord.User = None):
    target_user = user or interaction.user
    
    async with bot.db_pool.acquire() as conn:
        player = await conn.fetchrow('SELECT * FROM players WHERE user_id = $1', target_user.id)
        
        if not player:
            msg = "❌ У вас еще нет профиля!" if target_user == interaction.user else f"❌ У {target_user.mention} еще нет профиля!"
            await interaction.response.send_message(msg)
            return
        
        avg_speed_data = await conn.fetchrow('''
            SELECT avg_speed, total_blocks, total_time 
            FROM speed_leaderboard WHERE user_id = $1
        ''', target_user.id)
        
        best_rank = await conn.fetchval('''
            SELECT COUNT(*) + 1 FROM players WHERE best_speed > $1
        ''', player['best_speed'])
        
        avg_rank = None
        if avg_speed_data:
            avg_rank = await conn.fetchval('''
                SELECT COUNT(*) + 1 FROM speed_leaderboard WHERE avg_speed > $1
            ''', avg_speed_data['avg_speed'])
    
    embed = discord.Embed(
        title=f"🎮 Профиль игрока",
        color=discord.Color.from_rgb(88, 101, 242)
    )
    
    embed.set_author(
        name=target_user.display_name,
        icon_url=target_user.display_avatar.url
    )
    
    embed.set_thumbnail(url=target_user.display_avatar.url)
    
    # Общая статистика
    hours = int(player['total_time_spent'] // 3600)
    minutes = int((player['total_time_spent'] % 3600) // 60)
    time_str = f"{hours}ч {minutes}м" if hours > 0 else f"{minutes}м"
    
    general_stats = (
        f"```\n"
        f"Игр сыграно     │ {player['games_played']}\n"
        f"Блоков пройдено │ {player['total_blocks_cleared']}\n"
        f"Время в игре    │ {time_str}\n"
        f"```"
    )
    embed.add_field(
        name="📊 Общая статистика",
        value=general_stats,
        inline=False
    )
    
    # Рекорды
    records_text = ""
    
    records_text += f"**🏆 Лучшая скорость**\n"
    records_text += f"├ **{player['best_speed']:.3f}** блоков/сек\n"
    records_text += f"└ Место в топе: **#{best_rank}**\n\n"
    
    if avg_speed_data:
        records_text += f"**⚡ Средняя скорость**\n"
        records_text += f"├ **{avg_speed_data['avg_speed']:.3f}** блоков/сек\n"
        records_text += f"└ Место в топе: **#{avg_rank}**\n\n"
    
    if player['best_blocks_normal'] > 0:
        records_text += f"**🎮 Обычный режим**\n"
        records_text += f"└ Лучший забег: **{player['best_blocks_normal']}** блоков\n\n"
    
    if player['best_blocks_hardcore'] > 0:
        records_text += f"**💀 Хардкор режим**\n"
        records_text += f"└ Лучший забег: **{player['best_blocks_hardcore']}** блоков\n\n"
    
    embed.add_field(
        name="🏅 Рекорды",
        value=records_text,
        inline=False
    )
    
    # В среднем за игру
    if player['games_played'] > 0:
        avg_blocks_per_game = player['total_blocks_cleared'] / player['games_played']
        avg_time_per_game = player['total_time_spent'] / player['games_played']
        
        additional_stats = (
            f"```\n"
            f"Блоков за игру  │ {avg_blocks_per_game:.1f}\n"
            f"Времени за игру │ {avg_time_per_game:.1f}с\n"
            f"```"
        )
        embed.add_field(
            name="📈 В среднем за игру",
            value=additional_stats,
            inline=False
        )
    
    embed.set_footer(text=f"Играет с {player['created_at'].strftime('%d.%m.%Y')} • ID: {target_user.id}")
    embed.timestamp = datetime.now()
    
    await interaction.response.send_message(embed=embed)

@bot.event
async def on_ready():
    print(f'✅ Бот запущен как {bot.user}')
    print(f'📊 Серверов: {len(bot.guilds)}')
    print(f'⚡ База данных подключена')

@bot.event
async def on_thread_delete(thread):
    """Очистка при удалении треда"""
    if thread.id in bot.active_games:
        del bot.active_games[thread.id]

if __name__ == "__main__":
    bot.run(TOKEN)