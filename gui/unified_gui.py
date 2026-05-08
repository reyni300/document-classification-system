import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from PIL import Image, ImageTk
import os
import json
from pdf2image import convert_from_path

import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestrator.unified_orchestrator import UnifiedOrchestrator
from ocr.ocr_module import OCRModule
from llm.llm_agent import LLMAgent
from db.db_module import DatabaseModule
from agents.classification_agent import ClassificationAgent
from agents.validation_agent import ValidationAgent
from llm.llm_client import LLMClient
from translation.translator import translate_field
import yaml

with open("config.yaml", "r", encoding="utf-8") as f:
    config = yaml.safe_load(f)


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Обработка финансовых документов")
        self.geometry("1300x750")
        self.configure(bg="#e0e0e0")

        self.ocr = OCRModule()
        self.llm_agent = LLMAgent()
        self.db = DatabaseModule(config['database']['file'])
        self.classifier = ClassificationAgent(LLMClient())
        self.validator = ValidationAgent()

        self.orchestrator = UnifiedOrchestrator(
            ocr_module=self.ocr,
            llm_agent=self.llm_agent,
            db_module=self.db,
            classifier=self.classifier,
            validator=self.validator,
            translator_func=translate_field if config['app'].get('show_translation', True) else None
        )

        self.current_file = None
        self.current_image_tk = None
        self.rotation = 0
        self.current_result = None

        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill="both", expand=True, padx=8, pady=8)

        self.tab_process = tk.Frame(self.notebook, bg="#e0e0e0")
        self.tab_history = tk.Frame(self.notebook, bg="#e0e0e0")
        self.notebook.add(self.tab_process, text="ОБРАБОТКА")
        self.notebook.add(self.tab_history, text="ИСТОРИЯ")

        self.setup_process_tab()
        self.setup_history_tab()
        self.refresh_history()

    def setup_process_tab(self):
        left_frame = tk.Frame(self.tab_process, bg="#ffffff", relief="groove", bd=1)
        left_frame.pack(side="left", fill="both", expand=True, padx=10, pady=10)

        tk.Label(left_frame, text="ВЫБОР ДОКУМЕНТА", font=("Arial", 12, "bold"),
                 bg="#ffffff", fg="#000000").pack(pady=(15, 5))

        self.btn_select = tk.Button(left_frame, text="ВЫБРАТЬ ФАЙЛ", command=self.select_file,
                                    bg="#d9d9d9", fg="#000000", font=("Arial", 11, "bold"),
                                    padx=20, pady=8, relief="raised", bd=2)
        self.btn_select.pack(pady=10, padx=20, fill="x")

        self.file_label = tk.Label(left_frame, text="Файл не выбран", bg="#ffffff", fg="#555555",
                                   font=("Arial", 10), wraplength=250)
        self.file_label.pack(pady=5, padx=20, fill="x")

        self.btn_rotate = tk.Button(left_frame, text="⟳ ПОВЕРНУТЬ", command=self.rotate_image,
                                    bg="#d9d9d9", fg="#000000", font=("Arial", 10),
                                    padx=20, pady=6, relief="raised", bd=2, state="disabled")
        self.btn_rotate.pack(pady=5, padx=20, fill="x")

        tk.Frame(left_frame, bg="#cccccc", height=2).pack(fill="x", padx=20, pady=10)

        tk.Label(left_frame, text="ПРЕДПРОСМОТР", font=("Arial", 11, "bold"),
                 bg="#ffffff", fg="#000000").pack(pady=(10, 5))

        self.preview_area = tk.Label(left_frame, text="Здесь будет изображение",
                                     bg="#f5f5f5", fg="#666666", font=("Arial", 10),
                                     width=40, height=12, relief="sunken", bd=1)
        self.preview_area.pack(pady=10, padx=20, fill="both", expand=True)

        tk.Frame(left_frame, bg="#cccccc", height=2).pack(fill="x", padx=20, pady=10)

        self.btn_process = tk.Button(left_frame, text="ЗАПУСТИТЬ АНАЛИЗ", command=self.start_processing,
                                     bg="#2e7d32", fg="#ffffff", font=("Arial", 12, "bold"),
                                     padx=30, pady=10, relief="raised", bd=2, state="disabled")
        self.btn_process.pack(pady=15, padx=20, fill="x")


        right_frame = tk.Frame(self.tab_process, bg="#ffffff", relief="groove", bd=1)
        right_frame.pack(side="right", fill="both", expand=True, padx=10, pady=10)

        tk.Label(right_frame, text="РЕЗУЛЬТАТ АНАЛИЗА", font=("Arial", 12, "bold"),
                 bg="#ffffff", fg="#000000").pack(pady=(15, 5))

        self.result_text = tk.Text(right_frame, wrap="word", font=("Courier New", 10),
                                   bg="#fafafa", fg="#000000", relief="sunken", bd=1)
        self.result_text.pack(fill="both", expand=True, padx=15, pady=10)

        bottom_frame = tk.Frame(self.tab_process, bg="#ffffff", relief="groove", bd=1)
        bottom_frame.pack(side="bottom", fill="x", padx=10, pady=(0, 10))

        tk.Label(bottom_frame, text="ЛОГ ОБРАБОТКИ", font=("Arial", 10, "bold"),
                 bg="#ffffff", fg="#000000").pack(anchor="w", padx=15, pady=(10, 5))

        self.log_text = tk.Text(bottom_frame, wrap="word", font=("Courier New", 9),
                                bg="#f5f5f5", fg="#000000", relief="sunken", bd=1, height=6)
        self.log_text.pack(fill="both", expand=True, padx=15, pady=(0, 10))

        self.btn_export = tk.Button(bottom_frame, text="СОХРАНИТЬ КАК JSON", command=self.export_json,
                                    bg="#f57c00", fg="#ffffff", font=("Arial", 10, "bold"),
                                    padx=20, pady=6, relief="raised", bd=2, state="disabled")
        self.btn_export.pack(pady=(0, 10))

    def setup_history_tab(self):
        columns = ("ID", "Файл", "Дата", "Сумма", "Категория")
        self.history_tree = ttk.Treeview(self.tab_history, columns=columns, show="headings", height=18)

        self.history_tree.heading("ID", text="ID")
        self.history_tree.heading("Файл", text="Файл")
        self.history_tree.heading("Дата", text="Дата обработки")
        self.history_tree.heading("Сумма", text="Сумма")
        self.history_tree.heading("Категория", text="Категория")

        self.history_tree.column("ID", width=50)
        self.history_tree.column("Файл", width=400)
        self.history_tree.column("Дата", width=130)
        self.history_tree.column("Сумма", width=100)
        self.history_tree.column("Категория", width=130)

        scrollbar = ttk.Scrollbar(self.tab_history, orient="vertical", command=self.history_tree.yview)
        self.history_tree.configure(yscrollcommand=scrollbar.set)

        self.history_tree.pack(side="left", fill="both", expand=True, padx=10, pady=10)
        scrollbar.pack(side="right", fill="y", pady=10)

        btn_frame = tk.Frame(self.tab_history, bg="#e0e0e0")
        btn_frame.pack(fill="x", padx=10, pady=5)

        tk.Button(btn_frame, text="ПРОСМОТРЕТЬ", command=self.view_document,
                  bg="#1976d2", fg="#000000", font=("Arial", 10, "bold"),
                  padx=15, pady=5, relief="raised", bd=2).pack(side="left", padx=5)

        tk.Button(btn_frame, text="УДАЛИТЬ", command=self.delete_document,
                  bg="#c62828", fg="#000000", font=("Arial", 10, "bold"),
                  padx=15, pady=5, relief="raised", bd=2).pack(side="left", padx=5)

        tk.Button(btn_frame, text="ОБНОВИТЬ", command=self.refresh_history,
                  bg="#616161", fg="#000000", font=("Arial", 10, "bold"),
                  padx=15, pady=5, relief="raised", bd=2).pack(side="left", padx=5)

        self.history_tree.bind("<Double-1>", self.on_history_double_click)
        self.history_tree.bind("<<TreeviewSelect>>", lambda e: None)

    def on_history_double_click(self, event):
        selected = self.history_tree.selection()
        if not selected:
            return
        item = self.history_tree.item(selected[0])
        doc_id = item['values'][0]
        data = self.db.get_one(doc_id)
        if data:
            _, parsed = data
            win = tk.Toplevel(self)
            win.title(f"Документ ID {doc_id} - Просмотр")
            win.geometry("700x600")
            win.configure(bg="#e0e0e0")

            text = tk.Text(win, wrap="word", font=("Courier New", 10),
                           bg="#ffffff", fg="#000000")
            text.pack(fill="both", expand=True, padx=10, pady=10)
            text.insert("1.0", json.dumps(parsed, indent=2, ensure_ascii=False))
            text.config(state="disabled")

            tk.Button(win, text="ЗАКРЫТЬ", command=win.destroy,
                      bg="#d9d9d9", fg="#000000", font=("Arial", 10, "bold"),
                      padx=20, pady=5, relief="raised", bd=2).pack(pady=10)

    def select_file(self):
        file = filedialog.askopenfilename(
            filetypes=[("Изображения и PDF", "*.png *.jpg *.jpeg *.pdf"), ("Все файлы", "*.*")]
        )
        if file:
            self.current_file = file
            self.rotation = 0
            self.file_label.config(text=os.path.basename(file), fg="#2e7d32", font=("Arial", 10, "bold"))
            self.btn_process.config(state="normal")
            self.btn_rotate.config(state="normal")
            self.show_preview()
            self.log_text.delete("1.0", "end")
            self.result_text.delete("1.0", "end")
            self.log_text.insert("1.0", "✓ Файл выбран: " + os.path.basename(file) + "\n")
            self.log_text.insert("1.0", "▶ Нажмите 'ЗАПУСТИТЬ АНАЛИЗ'\n\n")

    def get_image(self):
        if self.current_file and self.current_file.lower().endswith(".pdf"):
            images = convert_from_path(self.current_file, first_page=1, last_page=1)
            img = images[0]
        elif self.current_file:
            img = Image.open(self.current_file)
        else:
            return None
        if self.rotation != 0:
            img = img.rotate(self.rotation, expand=True)
        return img

    def show_preview(self):
        try:
            img = self.get_image()
            if img:
                img.thumbnail((350, 250), Image.Resampling.LANCZOS)
                self.current_image_tk = ImageTk.PhotoImage(img)
                self.preview_area.config(image=self.current_image_tk, text="")
            else:
                self.preview_area.config(image="", text="Не удалось загрузить изображение")
        except Exception as e:
            self.preview_area.config(image="", text=f"Ошибка: {e}")

    def rotate_image(self):
        self.rotation = (self.rotation + 90) % 360
        self.show_preview()

    def start_processing(self):
        if not self.current_file:
            return

        self.btn_process.config(state="disabled", text="ОБРАБОТКА...")
        self.update_idletasks()

        try:
            result = self.orchestrator.process_document(self.current_file, os.path.basename(self.current_file))
            self.show_result(result)
        except Exception as e:
            self.show_error(str(e))
        finally:
            self.btn_process.config(state="normal", text="ЗАПУСТИТЬ АНАЛИЗ")

    def show_result(self, result):
        self.result_text.delete("1.0", "end")

        parsed = result.get('parsed', {})
        category = result.get('category', {}).get('category', 'N/A')
        validation = result.get('validation', {})

        output = f"""
╔════════════════════════════════════════════════════════════════╗
║                        РЕЗУЛЬТАТ АНАЛИЗА                       ║
╚════════════════════════════════════════════════════════════════╝

  Категория:      {category.upper()}
  Сумма:          {parsed.get('total', 'N/A')} {parsed.get('currency', 'RUB')}
  Дата:           {parsed.get('date', 'N/A')}
  Компания:       {parsed.get('company', 'N/A')}
  Номер счёта:    {parsed.get('invoice_number', 'N/A')}
  НДС:            {parsed.get('vat', 'N/A')}

╔════════════════════════════════════════════════════════════════╗
║                           ВАЛИДАЦИЯ                            ║
╚════════════════════════════════════════════════════════════════╝

  Статус:           {'ПРОЙДЕНА' if validation.get('valid') else '✗ НЕ ПРОЙДЕНА'}

"""
        if validation.get('errors'):
            output += "\n  Ошибки:\n"
            for err in validation['errors']:
                output += f"    • {err}\n"

        output += f"""
╔════════════════════════════════════════════════════════════════╗
║                               ID                               ║
╚════════════════════════════════════════════════════════════════╝

  Документ ID:      {result.get('doc_id', 'N/A')}
"""

        self.result_text.insert("1.0", output)

        self.log_text.delete("1.0", "end")
        for log in result.get('logs', []):
            self.log_text.insert("end", f"{log}\n")

        self.btn_export.config(state="normal")
        self.current_result = result
        self.refresh_history()

    def show_error(self, error):
        self.result_text.delete("1.0", "end")
        self.result_text.insert("1.0", f"ОШИБКА:\n{error}")
        self.log_text.insert("end", f"\nОшибка: {error}\n")

    def export_json(self):
        if hasattr(self, 'current_result') and self.current_result:
            file_path = filedialog.asksaveasfilename(
                defaultextension=".json",
                filetypes=[("JSON файлы", "*.json")],
                initialfile=f"result_{self.current_result.get('doc_id', 'unknown')}.json"
            )
            if file_path:
                with open(file_path, 'w', encoding='utf-8') as f:
                    json.dump(self.current_result, f, indent=2, ensure_ascii=False)
                messagebox.showinfo("Успех", "Результат сохранён")

    def refresh_history(self):
        for row in self.history_tree.get_children():
            self.history_tree.delete(row)

        docs = self.db.get_all()
        for doc in docs:
            doc_id, filename, created_at = doc
            data = self.db.get_one(doc_id)
            if data:
                _, parsed = data
                total = parsed.get('total', 'N/A')
                category = parsed.get('category', 'N/A')
                date_short = created_at[:10] if created_at else 'N/A'
                self.history_tree.insert("", "end", values=(doc_id, filename, date_short, total, category))

    def view_document(self):
        selected = self.history_tree.selection()
        if not selected:
            messagebox.showinfo("Информация", "Выберите документ")
            return
        item = self.history_tree.item(selected[0])
        doc_id = item['values'][0]
        data = self.db.get_one(doc_id)
        if data:
            _, parsed = data
            win = tk.Toplevel(self)
            win.title(f"Документ ID {doc_id} - Просмотр")
            win.geometry("700x600")
            win.configure(bg="#e0e0e0")

            text = tk.Text(win, wrap="word", font=("Courier New", 10),
                           bg="#ffffff", fg="#000000")
            text.pack(fill="both", expand=True, padx=10, pady=10)
            text.insert("1.0", json.dumps(parsed, indent=2, ensure_ascii=False))
            text.config(state="disabled")

            tk.Button(win, text="ЗАКРЫТЬ", command=win.destroy,
                      bg="#d9d9d9", fg="#000000", font=("Arial", 10, "bold"),
                      padx=20, pady=5, relief="raised", bd=2).pack(pady=10)

    def delete_document(self):
        selected = self.history_tree.selection()
        if not selected:
            messagebox.showinfo("Информация", "Выберите документ")
            return
        item = self.history_tree.item(selected[0])
        doc_id = item['values'][0]
        if messagebox.askyesno("Подтверждение", f"Удалить документ ID {doc_id}?"):
            self.db.delete(doc_id)
            self.refresh_history()
            messagebox.showinfo("Успех", "Документ удалён")


if __name__ == "__main__":
    app = App()
    app.mainloop()