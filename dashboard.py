import sys
import psutil
from pynvml import *
import pyqtgraph as pg
import wmi
import os
from PyQt6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, 
                             QLabel, QProgressBar, QGridLayout, QGroupBox, QFrame, 
                             QCheckBox, QScrollArea)
from PyQt6.QtCore import QTimer, Qt

# --- ESTILOS (QSS) ---
DARK_MODE_STYLESHEET = """
    QWidget {
        background-color: #2E2E2E;
        color: #E0E0E0;
        font-family: 'Segoe UI', Arial;
    }
    QScrollArea {
        background-color: #2E2E2E;
        border: none;
    }
    QWidget#scroll_content {
        background-color: #2E2E2E;
    }
    QGroupBox {
        font-size: 18px;
        font-weight: bold;
        color: #4B9BFF;
    }
    QCheckBox {
        font-size: 14px;
    }
    QLabel#shutdown_status {
        font-size: 14px;
        color: #FFB84C; /* Naranja */
    }
    QLabel#peak_label {
        font-size: 14px;
    }
    QLabel#top_proc_label {
        font-size: 14px;
    }
    QProgressBar {
        border: 1px solid #555;
        border-radius: 5px;
        text-align: center;
        background-color: #3E3E3E;
        min-height: 20px;
    }
    QProgressBar::chunk {
        border-radius: 5px;
    }
"""

class MonitorDashboard(QMainWindow):
    def __init__(self):
        super().__init__()

        self.setWindowTitle("Monitor de Recursos Gamin")
        self.resize(800, 850) # Tamaño inicial

        self.shutdown_armed = False
        self.fan_zero_counter = 0

        # --- Contadores y banderas para Picos ---
        self.cpu_count = psutil.cpu_count()
        self.cpu_high_flag = False
        self.gpu_high_flag = False
        self.vram_high_flag = False
        self.ram_high_flag = False
        self.cpu_peak_count = 0
        self.gpu_peak_count = 0
        self.vram_peak_count = 0
        self.ram_peak_count = 0
        
        self.drag_timer = QTimer(self)
        self.drag_timer.setSingleShot(True)
        self.drag_timer.timeout.connect(self.resume_updates)
        
        # --- Inicializar NVIDIA NVML ---
        try:
            nvmlInit()
            self.gpu_handle = nvmlDeviceGetHandleByIndex(0)
            try:
                self.gpu_name_str = nvmlDeviceGetName(self.gpu_handle).decode('utf-8')
            except AttributeError:
                self.gpu_name_str = nvmlDeviceGetName(self.gpu_handle)
        except NVMLError as error:
            print(f"Error al inicializar NVML: {error}")
            self.gpu_handle = None
            self.gpu_name_str = "NVIDIA GPU (Error)"

        # --- Inicializar contadores de Red ---
        net_io = psutil.net_io_counters()
        self.last_bytes_sent = net_io.bytes_sent
        self.last_bytes_recv = net_io.bytes_recv

        # --- Detectar discos con PSUTIL y WMI ---
        self.disk_widgets = {}
        self.drive_info_map = {} 
        self.physical_drives_psutil = [] 
        try:
            self.last_disk_io = psutil.disk_io_counters(perdisk=True)
            self.physical_drives_psutil = list(self.last_disk_io.keys())
            print(f"Discos detectados por psutil: {self.physical_drives_psutil}")

            c = wmi.WMI()
            
            for drive in c.Win32_DiskDrive():
                psutil_name = f"PhysicalDrive{drive.Index}"
                if psutil_name not in self.physical_drives_psutil:
                    continue 

                try:
                    rotation_rate = drive.RotationRate
                    drive_type = "SSD" if rotation_rate == 0 else "HDD"
                except Exception:
                    drive_type = "SSD" 

                letters = []
                for partition in drive.associators("Win32_DiskDriveToDiskPartition"):
                    for logical_disk in partition.associators("Win32_LogicalDiskToPartition"):
                        letters.append(logical_disk.DeviceID) 
                drive_letters = ", ".join(letters) 
                
                self.drive_info_map[psutil_name] = {
                    'index': drive.Index,
                    'letters': drive_letters,
                    'type': drive_type
                }
            print(f"Mapa de discos WMI: {self.drive_info_map}")
        except Exception as e:
            print(f"Error detectando discos (WMI o psutil): {e}")
            if not self.drive_info_map and self.physical_drives_psutil:
                for name in self.physical_drives_psutil:
                    self.drive_info_map[name] = {'index': '?', 'letters': name, 'type': '?'}

        self.prime_processes()

        # --- Datos para las gráficas (60s) ---
        self.plot_data_points = 60
        self.cpu_plot_data = [0] * self.plot_data_points
        self.gpu_plot_data = [0] * self.plot_data_points
        self.ram_plot_data = [0] * self.plot_data_points

        self.setStyleSheet(DARK_MODE_STYLESHEET)
        self.initUI()
        
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.actualizar_datos)
        self.timer.start(1000)

    def moveEvent(self, event):
        """ Se llama CADA VEZ que la ventana se mueve. """
        self.timer.stop()
        self.drag_timer.start(250)
        super().moveEvent(event)

    def resume_updates(self):
        """ Se llama 250ms después de que la ventana DEJA de moverse. """
        if not self.timer.isActive():
            self.actualizar_datos()
            self.timer.start(1000)

    def prime_processes(self):
        """ Llama a cpu_percent(None) en todos los procesos para "cebarlos". """
        for p in psutil.process_iter():
            try:
                p.cpu_percent(interval=None)
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                pass

    def _crear_plot_widget(self, title):
        plot_widget = pg.PlotWidget()
        plot_widget.setTitle(title)
        plot_widget.setYRange(0, 100)
        plot_widget.getAxis('bottom').setTicks([])
        plot_widget.getAxis('left').setPen(None)
        plot_widget.setBackground(None)
        return plot_widget

    def initUI(self):
        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setCentralWidget(self.scroll_area)

        scroll_content_widget = QWidget()
        scroll_content_widget.setObjectName("scroll_content")
        
        main_layout = QGridLayout(scroll_content_widget)
        
        main_layout.setColumnStretch(0, 1)
        main_layout.setColumnStretch(1, 2)

        # --- CPU (Fila 0) ---
        cpu_stats_group = QGroupBox("CPU")
        cpu_layout = QVBoxLayout()
        cpu_grid = QGridLayout()
        self.cpu_usage_label = QLabel("Uso: 0%")
        self.cpu_clock_label = QLabel("Reloj: 0.00 GHz")
        cpu_grid.addWidget(self.cpu_usage_label, 0, 0)
        cpu_grid.addWidget(self.cpu_clock_label, 0, 1)
        cpu_layout.addLayout(cpu_grid)
        self.cpu_usage_bar = QProgressBar()
        cpu_layout.addWidget(self.cpu_usage_bar)
        cpu_stats_group.setLayout(cpu_layout)
        self.cpu_plot = self._crear_plot_widget("Historial Uso CPU (60s)")
        self.cpu_plot_curve = self.cpu_plot.plot(self.cpu_plot_data, pen='c') 
        main_layout.addWidget(cpu_stats_group, 0, 0)
        main_layout.addWidget(self.cpu_plot, 0, 1)

        # --- GPU (Fila 1) ---
        gpu_stats_group = QGroupBox(f"GPU: {self.gpu_name_str}")
        gpu_layout = QVBoxLayout()
        gpu_grid = QGridLayout()
        self.gpu_temp_label = QLabel("Temp: 0°C")
        self.gpu_usage_label = QLabel("Uso: 0%")
        self.gpu_fan_label = QLabel("Fan: 0%")
        self.gpu_vram_label = QLabel("VRAM: 0%")
        self.gpu_clock_label = QLabel("Reloj: 0 MHz")
        self.gpu_power_label = QLabel("Consumo: 0 W")
        gpu_grid.addWidget(self.gpu_temp_label, 0, 0)
        gpu_grid.addWidget(self.gpu_usage_label, 0, 1)
        gpu_grid.addWidget(self.gpu_fan_label, 1, 0)
        gpu_grid.addWidget(self.gpu_vram_label, 1, 1)
        gpu_grid.addWidget(self.gpu_clock_label, 2, 0)
        gpu_grid.addWidget(self.gpu_power_label, 2, 1)
        gpu_layout.addLayout(gpu_grid)
        self.gpu_vram_bar = QProgressBar() 
        self.gpu_vram_bar.setRange(0, 100) 
        gpu_layout.addWidget(self.gpu_vram_bar)
        gpu_stats_group.setLayout(gpu_layout)
        self.gpu_plot = self._crear_plot_widget("Historial Uso GPU (60s)")
        self.gpu_plot_curve = self.gpu_plot.plot(self.gpu_plot_data, pen='#FFB84C') 
        main_layout.addWidget(gpu_stats_group, 1, 0)
        main_layout.addWidget(self.gpu_plot, 1, 1)

        # --- RAM (Fila 2) ---
        ram_stats_group = QGroupBox("RAM")
        ram_layout = QVBoxLayout()
        self.ram_usage_label = QLabel("RAM: 0%")
        self.ram_usage_bar = QProgressBar()
        ram_layout.addWidget(self.ram_usage_label)
        ram_layout.addWidget(self.ram_usage_bar)
        ram_stats_group.setLayout(ram_layout)
        self.ram_plot = self._crear_plot_widget("Historial Uso RAM (60s)")
        self.ram_plot_curve = self.ram_plot.plot(self.ram_plot_data, pen='#4CFFB8') 
        main_layout.addWidget(ram_stats_group, 2, 0)
        main_layout.addWidget(self.ram_plot, 2, 1)
        
        # --- Discos (Fila 3) ---
        disk_stats_group = QGroupBox("Unidades (Actividad)")
        self.disk_layout = QVBoxLayout() 
        for drive_name in self.physical_drives_psutil:
            if drive_name in self.drive_info_map:
                info = self.drive_info_map[drive_name]
                label_text = f"Unidad ({info['letters']}): 0.0%"
                disk_label = QLabel(label_text)
                disk_bar = QProgressBar()
                self.disk_layout.addWidget(disk_label)
                self.disk_layout.addWidget(disk_bar)
                self.disk_widgets[drive_name] = {'label': disk_label, 'bar': disk_bar, 'info': info}
        disk_stats_group.setLayout(self.disk_layout)
        main_layout.addWidget(disk_stats_group, 3, 0)

        # --- Red (Fila 3) ---
        net_stats_group = QGroupBox("Red")
        net_layout = QVBoxLayout()
        self.net_down_label = QLabel("Descarga: 0.00 MB/s")
        self.net_up_label = QLabel("Subida: 0.00 MB/s")
        net_layout.addWidget(self.net_down_label)
        net_layout.addWidget(self.net_up_label)
        net_layout.addStretch() 
        net_stats_group.setLayout(net_layout)
        main_layout.addWidget(net_stats_group, 3, 1)

        # --- Contador de Picos (Fila 4) ---
        peak_group = QGroupBox("Historial de Picos (+95%)")
        peak_layout = QVBoxLayout()
        self.cpu_peak_label = QLabel("CPU: Nunca")
        self.gpu_peak_label = QLabel("GPU (Uso): Nunca")
        self.vram_peak_label = QLabel("VRAM: Nunca")
        self.ram_peak_label = QLabel("RAM: Nunca")
        self.cpu_peak_label.setObjectName("peak_label")
        self.gpu_peak_label.setObjectName("peak_label")
        self.vram_peak_label.setObjectName("peak_label")
        self.ram_peak_label.setObjectName("peak_label")
        peak_layout.addWidget(self.cpu_peak_label)
        peak_layout.addWidget(self.gpu_peak_label)
        peak_layout.addWidget(self.vram_peak_label)
        peak_layout.addWidget(self.ram_peak_label)
        peak_layout.addStretch()
        peak_group.setLayout(peak_layout)
        main_layout.addWidget(peak_group, 4, 0)

        # --- Top Procesos (Fila 4) ---
        top_proc_group = QGroupBox("Top Procesos CPU")
        top_proc_layout = QVBoxLayout()
        self.top_proc_1 = QLabel("1. ...")
        self.top_proc_2 = QLabel("2. ...")
        self.top_proc_3 = QLabel("3. ...")
        self.top_proc_1.setObjectName("top_proc_label")
        self.top_proc_2.setObjectName("top_proc_label")
        self.top_proc_3.setObjectName("top_proc_label")
        top_proc_layout.addWidget(self.top_proc_1)
        top_proc_layout.addWidget(self.top_proc_2)
        top_proc_layout.addWidget(self.top_proc_3)
        top_proc_layout.addStretch()
        top_proc_group.setLayout(top_proc_layout)
        main_layout.addWidget(top_proc_group, 4, 1)

        # --- Apagado (Fila 5) ---
        shutdown_group = QGroupBox("Apagado Automático")
        shutdown_layout = QVBoxLayout()
        self.shutdown_checkbox = QCheckBox("Apagar cuando la GPU esté fría (Ventilador a 0% Durante 1 minuto)")
        self.shutdown_checkbox.toggled.connect(self.toggle_shutdown)
        self.shutdown_status_label = QLabel("Apagado: DESACTIVADO")
        self.shutdown_status_label.setObjectName("shutdown_status")
        shutdown_layout.addWidget(self.shutdown_checkbox)
        shutdown_layout.addWidget(self.shutdown_status_label)
        shutdown_group.setLayout(shutdown_layout)
        main_layout.addWidget(shutdown_group, 5, 0, 1, 2)

        main_layout.setRowStretch(6, 1)
        self.scroll_area.setWidget(scroll_content_widget)

    def toggle_shutdown(self, checked):
        # (Función sin cambios)
        self.shutdown_armed = checked
        if checked:
            print("Apagado automático ARMADO.")
            self.shutdown_status_label.setText("Apagado: ARMADO (esperando ventilador a 0%...)")
            self.shutdown_status_label.setStyleSheet("color: #FFB84C;")
        else:
            print("Apagado automático DESARMADO.")
            self.fan_zero_counter = 0 
            self.shutdown_status_label.setText("Apagado: DESACTIVADO")
            self.shutdown_status_label.setStyleSheet("color: #E0E0E0;")

    def trigger_shutdown(self):
        # (Función sin cambios)
        print("¡Disparando apagado del sistema en 1 segundo!")
        self.shutdown_armed = False
        self.shutdown_checkbox.setChecked(False)
        self.shutdown_status_label.setText("¡APAGANDO!")
        self.shutdown_status_label.setStyleSheet("color: #FF4C4C;")
        os.system("shutdown /s /t 1")
        self.close()

    def actualizar_datos(self):
        # --- CPU ---
        cpu_percent = psutil.cpu_percent()
        cpu_mhz = psutil.cpu_freq().current
        self.cpu_usage_label.setText(f"Uso: {cpu_percent}%")
        self.cpu_clock_label.setText(f"Reloj: {cpu_mhz/1000:.2f} GHz")
        self.cpu_usage_bar.setValue(int(cpu_percent))
        self.actualizar_estilo_barra_uso(self.cpu_usage_bar, cpu_percent)
        self.cpu_plot_data.pop(0) 
        self.cpu_plot_data.append(cpu_percent) 
        self.cpu_plot_curve.setData(self.cpu_plot_data) 

        # --- Lógica de Contador para CPU ---
        if cpu_percent > 95 and not self.cpu_high_flag:
            self.cpu_peak_count += 1
            # --- ¡¡AQUÍ EL CAMBIO!! ---
            texto = "vez" if self.cpu_peak_count == 1 else "veces"
            self.cpu_peak_label.setText(f"CPU: {self.cpu_peak_count} {texto}")
            self.cpu_high_flag = True
        elif cpu_percent < 90 and self.cpu_high_flag:
            self.cpu_high_flag = False

        # --- Lógica de Top Procesos ---
        proc_list = []
        try:
            for p in psutil.process_iter(['name']):
                try:
                    percent = p.cpu_percent(interval=None) / self.cpu_count
                    if percent > 0.1 and p.info['name'] != 'System Idle Process':
                        proc_list.append((percent, p.info['name']))
                except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                    pass
            
            sorted_list = sorted(proc_list, key=lambda x: x[0], reverse=True)
            
            top_3_labels = [self.top_proc_1, self.top_proc_2, self.top_proc_3]
            for i in range(3):
                if i < len(sorted_list):
                    percent, name = sorted_list[i]
                    top_3_labels[i].setText(f"{i+1}. {name}: {percent:.1f}%")
                else:
                    top_3_labels[i].setText(f"{i+1}. ...")

        except Exception as e:
            print(f"Error en Top Procesos: {e}")

        # --- GPU ---
        if self.gpu_handle:
            try:
                temp = nvmlDeviceGetTemperature(self.gpu_handle, NVML_TEMPERATURE_GPU)
                util = nvmlDeviceGetUtilizationRates(self.gpu_handle)
                fan = nvmlDeviceGetFanSpeed(self.gpu_handle) 
                vram = nvmlDeviceGetMemoryInfo(self.gpu_handle)
                vram_percent = int((vram.used / vram.total) * 100)
                clock = nvmlDeviceGetClockInfo(self.gpu_handle, NVML_CLOCK_GRAPHICS)
                power = nvmlDeviceGetPowerUsage(self.gpu_handle)

                self.gpu_temp_label.setText(f"Temp: {temp}°C")
                self.gpu_usage_label.setText(f"Uso: {util.gpu}%")
                self.gpu_fan_label.setText(f"Fan: {fan}%")
                self.gpu_vram_label.setText(f"VRAM: {vram_percent}%")
                self.gpu_clock_label.setText(f"Reloj: {clock} MHz")
                self.gpu_power_label.setText(f"Consumo: {power / 1000:.0f} W")
                
                self.gpu_vram_bar.setValue(vram_percent)
                self.actualizar_estilo_barra_uso(self.gpu_vram_bar, vram_percent)
                self.gpu_plot_data.pop(0)
                self.gpu_plot_data.append(util.gpu)
                self.gpu_plot_curve.setData(self.gpu_plot_data)

                # --- Lógica de Contador para GPU y VRAM ---
                if util.gpu > 95 and not self.gpu_high_flag:
                    self.gpu_peak_count += 1
                    # --- ¡¡AQUÍ EL CAMBIO!! ---
                    texto = "vez" if self.gpu_peak_count == 1 else "veces"
                    self.gpu_peak_label.setText(f"GPU (Uso): {self.gpu_peak_count} {texto}")
                    self.gpu_high_flag = True
                elif util.gpu < 90 and self.gpu_high_flag:
                    self.gpu_high_flag = False
                
                if vram_percent > 95 and not self.vram_high_flag:
                    self.vram_peak_count += 1
                    # --- ¡¡AQUÍ EL CAMBIO!! ---
                    texto = "vez" if self.vram_peak_count == 1 else "veces"
                    self.vram_peak_label.setText(f"VRAM: {self.vram_peak_count} {texto}")
                    self.vram_high_flag = True
                elif vram_percent < 90 and self.vram_high_flag:
                    self.vram_high_flag = False

                # Lógica de apagado
                if self.shutdown_armed:
                    if fan == 0:
                        self.fan_zero_counter += 1
                        remaining = 60 - self.fan_zero_counter
                        self.shutdown_status_label.setText(f"Apagado: Ventilador a 0%. Apagando en {remaining}s...")
                        if self.fan_zero_counter >= 60:
                            self.trigger_shutdown()
                    else:
                        self.fan_zero_counter = 0
                        self.shutdown_status_label.setText("Apagado: ARMADO (esperando ventilador a 0%...)")

            except NVMLError as e:
                if e.value == NVML_ERROR_NOT_SUPPORTED:
                    self.gpu_power_label.setText("Consumo: N/A")
                else:
                    self.gpu_temp_label.setText("Temp: Error")

        # --- RAM ---
        ram = psutil.virtual_memory()
        self.ram_usage_label.setText(f"RAM: {ram.percent}%")
        self.ram_usage_bar.setValue(int(ram.percent))
        self.actualizar_estilo_barra_uso(self.ram_usage_bar, ram.percent)
        self.ram_plot_data.pop(0)
        self.ram_plot_data.append(ram.percent)
        self.ram_plot_curve.setData(self.ram_plot_data)

        # --- Lógica de Contador para RAM ---
        if ram.percent > 95 and not self.ram_high_flag:
            self.ram_peak_count += 1
            # --- ¡¡AQUÍ EL CAMBIO!! ---
            texto = "vez" if self.ram_peak_count == 1 else "veces"
            self.ram_peak_label.setText(f"RAM: {self.ram_peak_count} {texto}")
            self.ram_high_flag = True
        elif ram.percent < 90 and self.ram_high_flag:
            self.ram_high_flag = False

        # --- Discos (Actividad) ---
        new_disk_io = psutil.disk_io_counters(perdisk=True)
        for drive_name in self.physical_drives_psutil:
            if drive_name not in self.disk_widgets:
                continue 
            try:
                old_io = self.last_disk_io[drive_name]
                new_io = new_disk_io[drive_name]
                delta_time = (new_io.read_time - old_io.read_time) + \
                             (new_io.write_time - old_io.write_time)
                percent = min(100.0, max(0.0, (delta_time / 1000.0) * 100.0))
                
                widgets = self.disk_widgets[drive_name]
                info = widgets['info']
                label_text = f"Unidad ({info['letters']}): {percent:.1f}%"
                widgets['label'].setText(label_text)
                widgets['bar'].setValue(int(percent))
                self.actualizar_estilo_barra_uso(widgets['bar'], percent)
            except KeyError:
                pass 
            except Exception as e:
                print(f"Error disco {drive_name}: {e}")
        self.last_disk_io = new_disk_io

        # --- Red ---
        net_io = psutil.net_io_counters()
        bytes_sent_s = net_io.bytes_sent - self.last_bytes_sent
        bytes_recv_s = net_io.bytes_recv - self.last_bytes_recv
        mb_recv_s = bytes_recv_s / 1024 / 1024
        mb_sent_s = bytes_sent_s / 1024 / 1024
        self.net_down_label.setText(f"Descarga: {mb_recv_s:.2f} MB/s")
        self.net_up_label.setText(f"Subida: {mb_sent_s:.2f} MB/s")
        self.last_bytes_sent = net_io.bytes_sent
        self.last_bytes_recv = net_io.bytes_recv

    # --- Funciones de estilo ---
    def actualizar_estilo_barra_uso(self, bar_widget, percent):
        if percent > 90: color = "#FF4C4C"
        elif percent > 70: color = "#FFB84C"
        else: color = "#4CFFB8"
        bar_widget.setStyleSheet(f"QProgressBar::chunk {{ background-color: {color}; border-radius: 5px; }}")

    def closeEvent(self, event):
        if self.gpu_handle:
            nvmlShutdown()
        print("Cerrando aplicación y limpiando NVML.")
        event.accept()

# --- Punto de entrada principal ---
if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = MonitorDashboard()
    window.show()
    sys.exit(app.exec())