import json
import os
from pathlib import Path
import queue
import sys
import threading
import time
import tkinter as tk
from app_version import VERSION
from tkinter import ttk, filedialog, messagebox, simpledialog
from save_core import (scan_emulator, scan_switch, match_saves, import_save, export_save,
                       account_scope, load_accounts, save_accounts, account_names, export_profile,
                       read_config, write_config, related, template_key, resolve_template,
                       scan_eden, has_save_content, eden_account_names, plan_eden, normalized)

BASE = Path(sys.executable).parent if getattr(sys, 'frozen', False) else Path(__file__).parent
RESOURCE = Path(getattr(sys, '_MEIPASS', BASE))


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f'Switch Save Bridge {VERSION}｜主機備份 ⇄ Ryubing / Eden')
        self.geometry('1200x820')
        self.minsize(1000, 720)
        self.option_add('*Font', ('Microsoft JhengHei UI', 10))
        self.events = queue.Queue()
        self.busy = False
        self.rows = {}
        self.saves = []
        self.accounts = {}
        self.account_labels = {}
        self.loaded_paths = None
        self.settings_error = None
        self.templates_error = None
        try:
            self.templates = json.loads((BASE / 'templates.json').read_text(encoding='utf-8')) if (BASE / 'templates.json').exists() else {}
            if not isinstance(self.templates, dict) or any(not isinstance(v, dict) or any(not isinstance(e, dict) or not isinstance(e.get('game'), str) or not isinstance(e.get('profile'), str) for e in v.values()) for v in self.templates.values()):
                raise ValueError('範本設定格式無效')
        except Exception as e:
            self.templates, self.templates_error = {}, str(e)
        try:
            self.settings = load_accounts(BASE / 'accounts.json')
        except Exception as e:
            self.settings = {}
            self.settings_error = str(e)
        self.names = json.loads((RESOURCE / 'titles.json').read_text(encoding='utf-8'))
        self.direction = tk.StringVar(value='Switch → 模擬器')
        self.config_path = BASE / 'config.ini'
        self.config_error = None
        self.config_timer = None
        self.regions = {}
        try:
            paths, self.regions = read_config(self.config_path, BASE)
        except Exception as e:
            paths, _ = read_config(BASE / 'missing-default-config.ini', BASE)
            self.config_error = str(e)
        (BASE / 'atmosphere_save').mkdir(exist_ok=True)
        self.switch = tk.StringVar(value=paths['switch'])
        self.backend = tk.StringVar(value=paths['backend'] if paths['backend'] in ('Ryubing', 'Eden') else 'Eden')
        self.backend_paths = {'Ryubing': paths['ryubing'], 'Eden': paths['eden']}
        self.active_backend = self.backend.get()
        self.emulator = tk.StringVar(value=self.backend_paths[self.active_backend])
        self.output = tk.StringVar(value=str(Path(self.switch.get()) / 'export'))
        self.switch.trace_add('write', lambda *args: self.output.set(str(Path(self.switch.get()) / 'export')))
        for var in (self.switch, self.emulator, self.output):
            var.trace_add('write', self.schedule_config_save)
        self.slot = tk.StringVar(value='0')
        self.game_filter = tk.StringVar(value='所有遊戲')
        self.search = tk.StringVar()
        from ui_theme import build_ui
        build_ui(self)
        self.protocol('WM_DELETE_WINDOW', self.close)
        self.after(100, self.poll)
        self.after(200, self.scan)
        if self.settings_error:
            self.after(300, lambda: messagebox.showerror('帳號設定無法讀取', self.settings_error + '\n原檔案已保留；請先修復或移走 EXE 旁 accounts.json。'))
        if self.config_error:
            self.after(300, lambda: messagebox.showerror('設定無法讀取', self.config_error + '\n原 config.ini 已保留，請修復後重新啟動。'))

    def close(self):
        if self.busy:
            messagebox.showinfo('作業中', '正在處理存檔，請等待本批次完成。')
        else:
            if self.config_timer:
                self.after_cancel(self.config_timer)
                self.config_timer = None
                self.save_config()
            self.destroy()

    def schedule_config_save(self, *args):
        if self.config_error:
            return
        if self.config_timer:
            self.after_cancel(self.config_timer)
        self.config_timer = self.after(1000, self.save_config)

    def save_config(self):
        self.config_timer = None
        if self.config_error:
            return
        try:
            if self.config_path.exists():
                _, self.regions = read_config(self.config_path, BASE)
            self.backend_paths[self.active_backend] = self.emulator.get()
            write_config(self.config_path, {'switch': self.switch.get(), 'emulator': self.backend_paths['Ryubing'], 'export': self.output.get(), 'ryubing': self.backend_paths['Ryubing'], 'eden': self.backend_paths['Eden'], 'backend': self.active_backend}, self.regions)
        except Exception as e:
            messagebox.showerror('儲存失敗', str(e))

    def show_help(self):
        messagebox.showinfo('存檔類型與 Ryubing 副本', 'Device 是整台主機共用的存檔，例如動物森友會的島嶼資料，不屬於 user1 或 user2。僅配對同遊戲的 Device 目的地，覆寫會影響所有共用玩家。\n\nRyubing 副本 0／1 是模擬器為提交與復原維護的內部副本，不是玩家 1／2。一般匯出選 0；1 供進階復原時手動選擇，不保證較新。操作前完全關閉模擬器。\n\n僅憑存檔無法可靠判定 JAP／USA／EUR。區域預設未知，可在 config.ini 的 [regions] 以 Title ID 手動標註；不代表已驗證存檔相容性。')

    def show_dbi_help(self):
        messagebox.showinfo('DBI 匯出至電腦', '1. 在 Atmosphère 的 Switch 啟動 DBI，用支援資料傳輸的 USB 線連接電腦，選 Run MTP responder。\n\n2. 在電腦檔案總管開啟 Switch／DBI 裝置 → 7: Saves，依 DBI 畫面找到遊戲清單（可能區分 Installed／Uninstalled）。\n\n3. 將遊戲名稱資料夾複製到本程式的 atmosphere_save；不要額外包一層 7: Saves 或 Installed games。複製完成後再結束 DBI MTP。\n\n4. 目錄應為 atmosphere_save／遊戲名稱／使用者名稱／存檔檔案，例如 遊戲名稱1／user1／save_data。user1 是範例，請保留 DBI 實際顯示的使用者名稱。Device 是共用存檔；BCAT、SD_Cache 會略過。\n\n5. 按重新掃描、設定帳號對照，選取存檔並開始轉檔。Ryujinx／Ryubing 未配對時，先選對帳號，於遊戲清單按右鍵「開啟使用者存檔資料夾」；Device 請選「開啟裝置存檔資料夾」。不需實際執行遊戲，完成後關閉模擬器再掃描。詳細範例請參考旁邊的使用說明。')

    def browse(self, var):
        if self.busy:
            return
        path = filedialog.askdirectory(initialdir=var.get() if Path(var.get()).is_dir() else str(BASE))
        if path:
            var.set(path)

    def change_backend(self, event=None):
        if self.busy:
            self.backend.set(self.active_backend)
            return
        self.backend_paths[self.active_backend] = self.emulator.get()
        self.active_backend = self.backend.get()
        self.emulator.set(self.backend_paths[self.active_backend])
        self.slot_choice.configure(state='disabled' if self.active_backend == 'Eden' else 'readonly')
        self.format_hint.set(self.format_description())
        self.game_filter.set('所有遊戲')
        self.scan()

    def format_description(self):
        return ('Eden：直接轉移存檔內容，保留模擬器容量資訊；不使用 0／1 副本。' if self.active_backend == 'Eden' else 'Ryubing：匯入同步寫入 0、1 副本。') + ' 個人與 Device 分開處理；轉檔前請完全關閉模擬器。'

    def say(self, text):
        self.log.configure(state='normal')
        self.log.insert('end', text + '\n')
        self.log.see('end')
        self.log.configure(state='disabled')

    def scan(self):
        if self.busy:
            return
        try:
            scanner = scan_eden if self.active_backend == 'Eden' else scan_emulator
            self.saves, self.names, warnings = scanner(self.emulator.get(), self.names)
            self.loaded_paths = (self.switch.get(), self.emulator.get(), self.direction.get())
            self.accounts = dict(self.settings.get(account_scope(self.switch.get(), self.emulator.get()), {}))
            self.eden_profiles = eden_account_names(self.emulator.get()) if self.active_backend == 'Eden' else None
            self.account_labels = (self.eden_profiles or {}) if self.active_backend == 'Eden' else account_names(self.emulator.get())
            for iid in self.rows:
                self.tree.delete(iid)
            self.rows.clear()
            if self.direction.get().startswith('Switch'):
                for game, profile, src in scan_switch(self.switch.get()):
                    matches = match_saves(game, profile, self.saves, self.names, self.accounts)
                    saved = self.templates.get(account_scope(self.switch.get(), self.emulator.get()), {})
                    confirmed = [s for s in self.saves if resolve_template(s, saved, self.switch.get(), self.accounts) == src]
                    if confirmed:
                        matches = confirmed
                    if not matches and self.active_backend == 'Eden':
                        ids = {tid for tid, name in self.names.items() if normalized(name) == normalized(game)}
                        import re
                        ids.update(x.lower() for x in re.findall(r'(?i)(?<![0-9a-f])010[0-9a-f]{13}(?![0-9a-f])', game))
                        ids.update(key.split(':')[0] for key, entry in saved.items() if entry['game'] == game and entry['profile'] == profile)
                        uid = '0' * 32 if profile == 'Device' else self.accounts.get(profile)
                        if len(ids) == 1 and uid:
                            try:
                                matches = [plan_eden(self.emulator.get(), next(iter(ids)), uid, 3 if profile == 'Device' else 1, game)]
                            except ValueError:
                                pass
                    target = matches[0] if len(matches) == 1 else None
                    status = ('可轉移（Device 共用存檔）' if profile == 'Device' else '可轉移（已核對帳號 UID）') if target else ('多份目的地，請手動配對' if matches else ('未配對：Eden 右鍵開啟存檔位置，再檢查帳號對照' if self.active_backend == 'Eden' else '未配對：右鍵開啟使用者存檔資料夾，再檢查帳號對照'))
                    import re
                    ids = {tid for tid, name in self.names.items() if normalized(name) == normalized(game)}
                    ids.update(x.lower() for x in re.findall(r'(?i)(?<![0-9a-f])010[0-9a-f]{13}(?![0-9a-f])', game))
                    detected = [s for s in self.saves if s.tid in ids and s.kind == (3 if profile == 'Device' else 1)]
                    if not target and detected and profile != 'Device' and profile not in self.accounts:
                        status = '已掃到遊戲；請設定「帳號對照」'
                    elif not target and detected:
                        status = '已掃到遊戲；請核對帳號 UID 或使用手動配對'
                    elif not target and not ids:
                        status = '名稱未識別；請用「手動配對」確認 Title ID'
                    mapped_uid = target.uid if target and target.kind == 1 else self.accounts.get(profile) if profile != 'Device' else None
                    if not target and self.active_backend == 'Ryubing' and profile == 'Device' and ids and not detected:
                        status = '未配對：右鍵開啟裝置存檔資料夾，再重新掃描'
                    label = profile + (' → ' + self.uid_label(mapped_uid) if mapped_uid else '')
                    if not target and not ids and mapped_uid:
                        status = '帳號已配對；缺少遊戲 Title ID，請手動配對'
                    if target and not target.path.exists():
                        status = '待自動建立 Eden 目的地（轉檔時確認 Title ID）'
                    region = self.regions.get(target.tid, '未知') + ('（手動）' if target and target.tid in self.regions else '') if target else '未知'
                    tid = target.tid if target else next(iter(ids)) if len(ids) == 1 else ''
                    iid = self.tree.insert('', 'end', values=(game, label, tid.upper() or '—', status, region))
                    self.rows[iid] = {'src': src, 'save': target, 'game': game, 'profile': profile, 'tid': tid}
            else:
                saved = self.templates.get(account_scope(self.switch.get(), self.emulator.get()), {})
                for s in self.saves:
                    nonempty = has_save_content(s, self.slot.get())
                    profile = export_profile(s, self.accounts)
                    template = resolve_template(s, saved, self.switch.get(), self.accounts)
                    if template:
                        profile = template.name
                    region = self.regions.get(s.tid, '未知') + ('（手動）' if s.tid in self.regions else '')
                    status = '可匯出 → ' + template.parent.name if template else '未確認主機範本：請先備份主機存檔並設定對照'
                    iid = self.tree.insert('', 'end', values=(s.title, profile, s.path.name, status if nonempty else '空白存檔', region))
                    self.rows[iid] = {'save': s, 'empty': not nonempty, 'game': s.title, 'profile': profile, 'template': template}
            games = sorted({r['game'] for r in self.rows.values()})
            self.games_combo.configure(values=['所有遊戲'] + games)
            if self.game_filter.get() not in games:
                self.game_filter.set('所有遊戲')
            self.filter_rows()
            total = len(self.saves)
            populated = sum(has_save_content(s, self.slot.get()) for s in self.saves)
            self.say(f'{self.active_backend} 掃描完成：{total} 個存檔目錄，其中 {populated} 個含遊戲資料，{total - populated} 個空白或只有容量資訊。')
            for warning in warnings:
                self.say('略過 ' + warning)
        except Exception as e:
            self.loaded_paths = None
            messagebox.showerror('掃描失敗', str(e))

    def uid_label(self, uid):
        label = self.account_labels.get(uid)
        if label is None:
            label = '不在目前 Eden 帳號清單，請核對' if self.active_backend == 'Eden' and self.eden_profiles is not None else '未知帳號'
        return f'{label} [{uid}]'

    def show_emulator_saves(self):
        if self.busy or not self.require_current():
            return
        dialog = tk.Toplevel(self)
        dialog.title(f'{self.active_backend} 已掃描存檔（唯讀）')
        dialog.geometry('1100x550')
        ttk.Label(dialog, text='空白目錄可作為匯入目的地，但沒有可匯出的遊戲進度；帳號不在清單時請先核對。', padding=10).pack(anchor='w')
        frame = ttk.Frame(dialog)
        frame.pack(fill='both', expand=True, padx=10, pady=10)
        tree = ttk.Treeview(frame, columns=('title', 'account', 'status', 'path'), show='headings')
        for key, label, width in [('title', '遊戲名稱／Title ID', 265), ('account', '帳號／Device', 300), ('status', '內容', 155), ('path', '存檔路徑', 600)]:
            tree.heading(key, text=label)
            tree.column(key, width=width, stretch=True)
        sy = ttk.Scrollbar(frame, orient='vertical', command=tree.yview)
        sx = ttk.Scrollbar(frame, orient='horizontal', command=tree.xview)
        tree.configure(yscrollcommand=sy.set, xscrollcommand=sx.set)
        sy.pack(side='right', fill='y'); sx.pack(side='bottom', fill='x'); tree.pack(fill='both', expand=True)
        for s in self.saves:
            account = 'Device（共用）' if s.kind == 3 else self.uid_label(s.uid)
            tree.insert('', 'end', values=(f'{s.title} [{s.tid.upper()}]', account, '有遊戲存檔' if has_save_content(s, self.slot.get()) else '空白／只有容量資訊', str(s.path)))
        ttk.Button(dialog, text='關閉', command=dialog.destroy).pack(pady=8)

    def edit_template(self):
        if self.busy or not self.require_current():
            return
        if self.templates_error:
            messagebox.showerror('範本設定無法讀取', self.templates_error + '\n請修復或移走 templates.json 後重新啟動；原檔案已保留。')
            return
        selected = self.tree.selection()
        if self.direction.get().startswith('Switch') or len(selected) != 1:
            messagebox.showinfo('設定主機範本', '請切換至「模擬器 → Switch 備份」，選取一個遊戲存檔，再設定主機範本。')
            return
        save = self.rows[selected[0]]['save']
        try:
            candidates = [(g, p, path) for g, p, path in scan_switch(self.switch.get()) if (p == 'Device' if save.kind == 3 else p != 'Device' and self.accounts.get(p) == save.uid)]
        except Exception as e:
            messagebox.showerror('讀取範本失敗', str(e))
            return
        if not candidates:
            messagebox.showinfo('找不到主機範本', '請先在 Switch 以正確使用者執行遊戲並建立存檔，用 DBI 備份到 atmosphere_save，再設定帳號對照。Device 需有同類型範本。')
            return
        dialog = tk.Toplevel(self)
        dialog.title('確認主機範本：' + save.title)
        dialog.geometry('820x240')
        ttk.Label(dialog, text=f'模擬器：{save.title}\nTitle ID：{save.tid}\n請確認選到同一款遊戲的主機備份；名稱相似不代表存檔相容。', padding=12).pack(anchor='w')
        choice = ttk.Combobox(dialog, state='readonly', values=[g + '／' + p for g, p, _ in candidates], width=95)
        choice.pack(fill='x', padx=12)
        guess = [i for i, (g, p, _) in enumerate(candidates) if save in match_saves(g, p, [save], self.names, self.accounts)]
        choice.current(guess[0] if len(guess) == 1 else 0)
        def apply():
            game, profile, _ = candidates[choice.current()]
            if not messagebox.askyesno('確認遊戲與帳號', f'{save.title} [{save.tid}]\n→ {game}／{profile}\n\n確認為同一款遊戲及正確使用者，儲存雙向對照？', parent=dialog):
                return
            scope = account_scope(self.switch.get(), self.emulator.get())
            updated = dict(self.templates)
            updated[scope] = dict(updated.get(scope, {}))
            updated[scope][template_key(save)] = {'game': game, 'profile': profile}
            try:
                save_accounts(BASE / 'templates.json', updated)
            except Exception as e:
                messagebox.showerror('儲存失敗', str(e), parent=dialog)
                return
            self.templates = updated
            dialog.destroy()
            self.scan()
        ttk.Button(dialog, text='確認並儲存對照', style='Setting.TButton', command=apply).pack(pady=12)
        dialog.transient(self)
        dialog.grab_set()

    def require_current(self):
        if self.loaded_paths != (self.switch.get(), self.emulator.get(), self.direction.get()):
            messagebox.showinfo('請重新載入', '路徑或方向已變更，請先按「重新掃描」。')
            return False
        return True

    def filter_rows(self):
        if self.busy:
            return
        self.tree.selection_remove(*self.tree.selection())
        game, query = self.game_filter.get(), self.search.get().strip().casefold()
        visible = 0
        for iid, row in self.rows.items():
            searchable = row['game'] + ' ' + (row['save'].tid if row.get('save') else row.get('tid', ''))
            if (game == '所有遊戲' or row['game'] == game) and query in searchable.casefold():
                self.tree.move(iid, '', 'end')
                tags = ['even'] if visible % 2 else []
                tags.append('ready' if row.get('save') and not row.get('empty') else 'pending')
                self.tree.item(iid, tags=tags)
                visible += 1
            else:
                self.tree.detach(iid)
        self.status.set(f'顯示 {visible}／{len(self.rows)} 項；Ctrl／Shift 可多選。「全選可轉移」只選目前顯示項目。')

    def clear_filter(self):
        if not self.busy:
            self.game_filter.set('所有遊戲')
            self.search.set('')

    def edit_accounts(self):
        if self.busy or not self.require_current():
            return
        if self.settings_error:
            messagebox.showerror('設定錯誤', '請先修復或移走 accounts.json 再重新啟動；不會覆蓋無法讀取的設定。')
            return
        try:
            profiles = sorted({p for _, p, _ in scan_switch(self.switch.get()) if p != 'Device' and not p.startswith('User_')} | set(self.accounts))
        except Exception as e:
            messagebox.showerror('無法讀取 Switch 帳號', str(e))
            return
        uids = sorted(self.eden_profiles) if self.active_backend == 'Eden' and self.eden_profiles is not None else sorted({s.uid for s in self.saves if s.kind == 1})
        if not profiles or not uids:
            messagebox.showinfo('沒有可設定帳號', '請載入含使用者備份的 Switch 目錄，並先在模擬器以各帳號建立至少一份遊戲存檔。')
            return
        dialog = tk.Toplevel(self)
        dialog.title('帳號對照｜套用至所有遊戲')
        dialog.geometry('880x520')
        ttk.Label(dialog, text='每個 Switch 使用者指定一個模擬器帳號。未指定不會自動匯入；Device 不使用帳號對照。\n設定僅適用目前這一組 Switch／模擬器目錄；UID 以存檔中識別碼為準。', padding=12).pack(anchor='w')
        footer = ttk.Frame(dialog, padding=12)
        footer.pack(side='bottom', fill='x')
        content = ttk.Frame(dialog)
        content.pack(fill='both', expand=True)
        canvas = tk.Canvas(content, highlightthickness=0)
        scroll = ttk.Scrollbar(content, orient='vertical', command=canvas.yview)
        scroll.pack(side='right', fill='y')
        canvas.pack(fill='both', expand=True)
        canvas.configure(yscrollcommand=scroll.set)
        body = ttk.Frame(canvas, padding=12)
        canvas.create_window((0, 0), window=body, anchor='nw')
        body.bind('<Configure>', lambda e: canvas.configure(scrollregion=canvas.bbox('all')))
        choices = ['未指定'] + [self.uid_label(uid) for uid in uids]
        fields = {}
        for profile in profiles:
            line = ttk.Frame(body)
            line.pack(fill='x', pady=5)
            ttk.Label(line, text=profile, width=20).pack(side='left')
            choice = ttk.Combobox(line, values=choices, state='readonly', width=65)
            choice.pack(side='left')
            current = self.accounts.get(profile)
            choice.current(uids.index(current) + 1 if current in uids else 0)
            fields[profile] = choice
        def apply():
            mapped = {name: uids[c.current() - 1] for name, c in fields.items() if c.current() > 0}
            summary = '\n'.join(f'{name} → {self.uid_label(uid)}' for name, uid in mapped.items()) or '全部設為未指定'
            if len(set(mapped.values())) < len(mapped):
                summary += '\n\n注意：多個 Switch 使用者指向同一模擬器帳號。重複目的地仍不能同批覆寫；匯出將保留 User_UID 名稱。'
            if not messagebox.askyesno('儲存帳號對照', summary + '\n\n套用至目前目錄的所有遊戲？', parent=dialog):
                return
            updated = dict(self.settings)
            updated[account_scope(self.switch.get(), self.emulator.get())] = mapped
            try:
                save_accounts(BASE / 'accounts.json', updated)
            except Exception as e:
                messagebox.showerror('儲存失敗', str(e), parent=dialog)
                return
            self.settings = updated
            dialog.destroy()
            self.scan()
        ttk.Button(footer, text='儲存並套用', style='Setting.TButton', command=apply).pack(side='right')
        dialog.transient(self)
        dialog.grab_set()

    def select_ready(self):
        if not self.busy:
            self.tree.selection_set([i for i in self.tree.get_children() if self.rows[i].get('save') and not self.rows[i].get('empty') and (self.direction.get().startswith('Switch') or self.rows[i].get('template'))])

    def map_selected(self):
        if self.busy or not self.direction.get().startswith('Switch'):
            return
        if not self.require_current():
            return
        selected = self.tree.selection()
        if len(selected) != 1:
            messagebox.showinfo('手動配對', '請先選取一項 Switch 存檔。')
            return
        row = self.rows[selected[0]]
        kind = 3 if row['profile'] == 'Device' else 1
        uid = self.accounts.get(row['profile'])
        if row['profile'].startswith('User_'):
            uid = row['profile'][5:].lower()
        if kind == 1 and not uid:
            messagebox.showinfo('先設定帳號', '請先開啟「帳號對照」指定這位使用者的模擬器帳號。')
            return
        candidates = [s for s in self.saves if s.kind == kind and (kind == 3 or s.uid == uid)]
        if self.active_backend == 'Eden':
            tid = simpledialog.askstring('確認遊戲 Title ID', '輸入這份 Switch 備份對應的 16 位 Title ID（可從 DBI 遊戲資訊核對）。\n不存在的 Eden 目的地將在開始轉檔後建立。', parent=self)
            if not tid:
                return
            try:
                s = plan_eden(self.emulator.get(), tid.strip(), uid or '0' * 32, kind, row['game'])
            except Exception as e:
                messagebox.showerror('無法指定目的地', str(e))
                return
            if messagebox.askyesno('確認遊戲對照', f'{row["game"]}／{row["profile"]}\nTitle ID：{s.tid}\n目的地：{s.path}\n\n確認同一款遊戲及正確帳號？'):
                if self.templates_error:
                    messagebox.showerror('範本設定錯誤', self.templates_error)
                    return
                scope = account_scope(self.switch.get(), self.emulator.get())
                updated = dict(self.templates)
                updated[scope] = dict(updated.get(scope, {}))
                updated[scope][template_key(s)] = {'game': row['game'], 'profile': row['profile']}
                try:
                    save_accounts(BASE / 'templates.json', updated)
                except Exception as e:
                    messagebox.showerror('儲存對照失敗', str(e))
                    return
                self.templates = updated
                row['save'] = s
                self.tree.item(selected[0], values=(row['game'], row['profile'], s.tid.upper(), '已確認；轉檔時建立或匯入', '未知'))
            return
        if not candidates:
            messagebox.showinfo('沒有目的地', '請先在模擬器建立對應類型的存檔，再重新掃描。')
            return
        dialog = tk.Toplevel(self)
        dialog.title('手動配對：' + row['game'] + '／' + row['profile'])
        dialog.geometry('900x260')
        ttk.Label(dialog, text='請確認遊戲版本、地區與使用者；錯誤配對可能使遊戲無法讀取。', padding=14).pack(anchor='w')
        labels = [f'{s.title} [{s.tid}] | {s.path.name} | {s.uid}' for s in candidates]
        choice = ttk.Combobox(dialog, values=labels, state='readonly', width=120)
        choice.pack(padx=14, fill='x')
        choice.current(0)
        def apply():
            s = candidates[choice.current()]
            if messagebox.askyesno('確認配對', f'{row["game"]}／{row["profile"]}\n→ {s.title}\nTitle ID：{s.tid}\n使用者：{s.uid}\n\n確定這是同一款遊戲及正確帳號？', parent=dialog):
                row['save'] = s
                region = self.regions.get(s.tid, '未知') + ('（手動）' if s.tid in self.regions else '')
                self.tree.item(selected[0], values=(row['game'], row['profile'], s.path.name, '手動配對（本次掃描）', region))
                dialog.destroy()
        ttk.Button(dialog, text='套用配對', style='Setting.TButton', command=apply).pack(pady=20)
        dialog.transient(self)
        dialog.grab_set()

    def execute(self):
        if self.busy or not self.require_current():
            return
        selected = [dict(self.rows[i]) for i in self.tree.selection()]
        if not selected or any(not r.get('save') for r in selected):
            messagebox.showinfo('選取項目', '請選取已完成配對的存檔。')
            return
        importing = self.direction.get().startswith('Switch')
        if not importing:
            saved = self.templates.get(account_scope(self.switch.get(), self.emulator.get()), {})
            for row in selected:
                template = resolve_template(row['save'], saved, self.switch.get(), self.accounts)
                if not template:
                    messagebox.showinfo('尚未確認主機目的地', '請先從 Switch 用 DBI 備份該遊戲存檔，設定帳號對照及「主機範本對照」，才能匯出。')
                    return
                row['template'], row['profile'] = template, template.name
            if len({r['template'] for r in selected}) != len(selected):
                messagebox.showerror('重複主機目的地', '多份模擬器存檔對應同一主機遊戲／使用者範本，請分開處理或修正對照。')
                return
        if not importing and (Path(self.output.get()).resolve() != Path(self.switch.get()).resolve() / 'export' or related(self.output.get(), self.emulator.get())):
            messagebox.showerror('匯出位置無效', '匯出目的地須為 Switch 備份資料夾下的 export，且不能與模擬器 save 相互包含。請確認沒有目錄連結。')
            return
        if importing and len({r['save'].path for r in selected}) != len(selected):
            messagebox.showerror('重複目的地', '多份來源指向同一個模擬器存檔。每批次每個目的地只能選一份來源。')
            return
        details = '\n'.join(f'{r["game"]}／{r["profile"]} → {r["save"].path.name}／{self.uid_label(r["save"].uid) if r["save"].kind == 1 else "Device"}' if importing else f'{r["save"].title} [{r["save"].tid}] → {r["template"].parent.name}／{r["template"].name}' for r in selected)
        message = (f'將覆寫 {len(selected)} 個 {self.active_backend} 存檔' + ('的 0、1 副本。' if self.active_backend == 'Ryubing' else '。') + '\n每項覆寫前均完整備份。' if importing else f'將匯出 {len(selected)} 個存檔' + (f'，讀取副本 {self.slot.get()}。' if self.active_backend == 'Ryubing' else '，直接讀取 Eden 存檔內容。') + f'\n輸出：{self.output.get()}\n不覆寫既有輸出。')
        if importing and any(r['save'].kind == 3 for r in selected):
            message += '\n\n包含 Device：這是共用存檔，覆寫會影響該遊戲所有共用玩家。'
        if not messagebox.askyesno('確認批次轉移', message + '\n\n' + details + '\n\n請確認模擬器已完全關閉，是否執行？'):
            return
        if importing and self.active_backend == 'Eden':
            if self.templates_error:
                messagebox.showerror('範本設定錯誤', self.templates_error)
                return
            scope = account_scope(self.switch.get(), self.emulator.get())
            updated = dict(self.templates)
            updated[scope] = dict(updated.get(scope, {}))
            for r in selected:
                updated[scope][template_key(r['save'])] = {'game': r['game'], 'profile': r['profile']}
            try:
                save_accounts(BASE / 'templates.json', updated)
            except Exception as e:
                messagebox.showerror('無法儲存已確認對照', str(e))
                return
            self.templates = updated
        self.busy = True
        self.progress.configure(maximum=len(selected), value=0)
        self.status.set('正在轉移及驗證，請勿啟動模擬器…')
        backup = BASE / 'Backups' / (time.strftime('%Y%m%d-%H%M%S') + '-' + os.urandom(3).hex())
        output, slot = str(Path(self.output.get()) / (time.strftime('%Y%m%d-%H%M%S') + '-' + os.urandom(3).hex())), self.slot.get()
        def worker():
            records = []
            for n, row in enumerate(selected, 1):
                try:
                    result = import_save(row['src'], row['save'], backup) if importing else export_save(row['save'], output, slot, row['profile'], row['template'])
                    record = f'成功 {row["save"].title}：{result}'
                except Exception as e:
                    record = f'失敗 {row["save"].title}：{e}'
                records.append(record)
                self.events.put(('step', n, record))
            try:
                backup.mkdir(parents=True, exist_ok=True)
                (backup / 'batch.log').write_text('\n'.join(records), encoding='utf-8')
            except Exception as e:
                self.events.put(('step', len(selected), f'紀錄寫入失敗：{e}'))
            self.events.put(('done', sum(x.startswith('成功') for x in records), len(records)))
        threading.Thread(target=worker, daemon=False).start()

    def poll(self):
        try:
            while True:
                event = self.events.get_nowait()
                if event[0] == 'step':
                    self.progress.configure(value=event[1])
                    self.say(event[2])
                else:
                    self.busy = False
                    self.filter_rows()
                    self.status.set(f'完成：成功 {event[1]}／{event[2]}。詳細結果見紀錄。')
                    messagebox.showinfo('批次完成', self.status.get())
        except queue.Empty:
            pass
        self.after(100, self.poll)

    def open_backups(self):
        path = BASE / 'Backups'
        path.mkdir(exist_ok=True)
        os.startfile(path)


if __name__ == '__main__':
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    app = App()
    if len(sys.argv) == 3 and sys.argv[1] == '--self-test':
        app.update()
        app.scan()
        report = {'import_rows': len(app.rows), 'matched': sum(bool(r.get('save')) for r in app.rows.values())}
        app.direction.set('模擬器 → Switch 備份')
        app.scan()
        report['export_rows'] = len(app.rows)
        if app.rows:
            app.game_filter.set(next(iter(app.rows.values()))['game'])
            app.filter_rows()
            app.select_ready()
            report['filtered_rows'] = len(app.tree.get_children())
            report['selected_visible_only'] = all(i in app.tree.get_children() for i in app.tree.selection())
        report['version'] = VERSION
        Path(sys.argv[2]).write_text(json.dumps(report), encoding='utf-8')
        app.destroy()
    else:
        app.mainloop()
