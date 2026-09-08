"""Double-click launcher (Python with Tkinter required)."""

import traceback
import tkinter as tk
from tkinter import messagebox

if __name__ == "__main__":
    try:
        from gripper_ota_tcp_gui import main
        main()
    except Exception:
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror("夹爪以太网 OTA 工具启动失败", traceback.format_exc(), parent=root)
        root.destroy()
