#Cliente
import os
import sys
import time
import struct
import serial
import crcmod
import threading
import subprocess
from collections import defaultdict, deque
from threading import Lock
import math
import tkinter as tk
from tkinter import ttk, messagebox
from datetime import datetime

sys.stdout.reconfigure(encoding='utf-8')

PORTA_SERIAL = 'COM7'
BAUD_RATE = 19200
TAMANHO_MAX = 245
MAX_RETRIES = 3
TIMEOUT_ACK = 5
pasta = "output"

serial_write_lock = Lock()
ack_buffer = deque()
pacotes_buffer = deque()

listaficheiros = {}
dummy_data = []

pedido_id = 0

####################################################
################ Camada de Ligação #################
####################################################


# CRC
crc16 = crcmod.predefined.mkCrcFun('crc-ccitt-false')

# Criar o pacote a ser enviado ligação + payload + crc
def criar_pacote_ligacao(payload_aplicacao, id_pedido, seq):
    payload_len = len(payload_aplicacao)
    crc = crc16(payload_aplicacao)
    header = struct.pack('<BBBBB', 0x7E, ord('D'), id_pedido, seq, payload_len)
    crc_bytes = struct.pack('<H', crc)
    return header + payload_aplicacao + crc_bytes

#Enviar ack ou nack
def enviar_ack_nack(ser, tipo, id_pedido, seq):
    pacote = bytearray([0x7E, ord(tipo), id_pedido, seq, 0])
    with serial_write_lock:
        ser.write(pacote)
    log(f"📤 Enviado {'ACK' if tipo == 'A' else 'NACK'} -> ID={id_pedido}, SEQ={seq}")

# Lê um numero especifico de bytes
def ler_bytes(ser, tamanho):
    dados = bytearray()
    while len(dados) < tamanho:
        parte = ser.read(1)
        if not parte:
            break
        dados.extend(parte)
    return dados

# Timeout para Ack/Nack
def esperar_resposta_ack(expected_id, expected_seq):
    inicio = time.time()
    while time.time() - inicio < TIMEOUT_ACK:
        for ack in list(ack_buffer):
            tipo, ack_id, ack_seq = ack
            if ack_id == expected_id and ack_seq == expected_seq:
                ack_buffer.remove(ack)
                return tipo
    return None

# Reenvio dos pacotes X vezes enquanto que não for recebido um Ack
def confirm_pacote(ser, payload_aplicacao, id_pedido, seq, id_pacote): # id_pacote aqui só é usado para o print
    pacote = criar_pacote_ligacao(payload_aplicacao, id_pedido, seq)
    for tentativa in range(MAX_RETRIES):
        with serial_write_lock:
            ser.write(pacote)
        log(f"📦 Enviado pacote ID_pacote={id_pacote}, ID_pedido={id_pedido}, Seq={seq}, Tamanho dos Dados = {len(pacote) - 10} bytes")
        #Timeout
        ack = esperar_resposta_ack(id_pedido, seq)
        if ack == ord('A'):
            log(f"✅ ACK recebido (ID_pedido={id_pedido}, Seq={seq})")
            return True
        elif ack == ord('N'):
            log(f"🔁 NACK recebido - reenvio (tentativa {tentativa + 1})")
        else:
            log(f"⏱️ Timeout esperando ACK/NACK")
    log("❌ Máximo de tentativas alcançado")
    return False

####################################################
################ Camada de Aplicação ###############
####################################################

#Cria o pacote de aplicação Tipo + Id_ pacote + Tamanho + dados
def criar_payload_aplicacao(dados, id_pacote, tipo):
    tamanho_dados = len(dados)
    if( dados == "".encode('utf-8')):
        tamanho_dados = 0
    return struct.pack('<BBB', tipo, id_pacote, tamanho_dados) + dados

# Retirar os dados da camada da aplicação
def dividir_pacote_aplicacao(payload):
    tipo, n_pacote, tamanho_dados = struct.unpack('<BBB', payload[:3])
    return n_pacote, payload[3:3 + tamanho_dados]

# Agrega os dados todos de um pedido

def construir_dados(npacotes, ID_pedido):
    dados = bytearray()
    while npacotes > 0:
        if not pacotes_buffer:
            time.sleep(0.1)
            continue

        for pacote in list(pacotes_buffer):
            id_pedido, seq, payload = pacote
            if id_pedido == ID_pedido:
                id_pacote, res = dividir_pacote_aplicacao(payload)
                dados.extend(res)
                pacotes_buffer.remove(pacote)
                log(f"📥 Pacote recebido ID_Pedido={id_pedido}, ID_Pacote={id_pacote}, SEQ={seq}")
                enviar_ack_nack(ser, 'A', id_pedido, seq)
                npacotes -= 1
                break
    return dados

# Recebe e devolve o tamanho (É usado após pedir-se o tamanho de um texto)

def receber_tamanho(pedido_id):
    while True:
        if not pacotes_buffer:
            time.sleep(0.1)
            continue
        for pacote in list(pacotes_buffer):
            id_pedido, seq, payload = pacote
            if id_pedido == pedido_id:
                log(f"📥 Pacote recebido ID_Pedido={id_pedido}, ID_Pacote={0}, SEQ={seq}  (Tamanho)")
                npacote , res = dividir_pacote_aplicacao(payload) 
                pacotes_buffer.remove(pacote)
                enviar_ack_nack(ser, 'A', id_pedido, seq)
                return int(res.decode('utf-8'))



################ Transferência de um ficheiro

# Pedido + receção dos dados com envio de acks + guardar estes dados num ficheiro na pasta do cliente
def tratamento_pedido(ser, nome_ficheiro, ID_pedido, size):

    payload_aplicacao = criar_payload_aplicacao(nome_ficheiro.encode('utf-8'),0, ord('D'))
    log1(f"📨 Requisição do ficheiro {nome_ficheiro}. (ID Pedido = {ID_pedido})")

    if not confirm_pacote(ser, payload_aplicacao, ID_pedido, 0, 0):
        log1(f"Não existe conexão com o servidor. (ID Pedido = {pedido_id})")
        return

    npacotes = math.ceil(size / TAMANHO_MAX)
    dados = construir_dados(npacotes, ID_pedido)

    log("")

    if dados:
        output_path = os.path.join(pasta, nome_ficheiro)
        os.makedirs(pasta, exist_ok=True)
        with open(output_path, 'wb') as f:
            f.write(dados)
        log1(f"💾 Arquivo salvo como: {output_path} (ID Pedido = {ID_pedido})")
        threading.Thread(target=mostrar_popup_e_abrir_ficheiro, args=(output_path,), daemon=True).start()
    else:
        log1("⚠️ Nenhum dado recebido para " + nome_ficheiro)

# Cada pedido de transferência é tratado individualmente
def transferir_ficheiro(nome_ficheiro, ser):
        global pedido_id
        pedido_id += 1
        size = listaficheiros[nome_ficheiro]
        threading.Thread(target=tratamento_pedido, args=(ser, nome_ficheiro, pedido_id, size), daemon=True).start()



################ Pedir Lista de ficheiros disponiveis

# Envio e receção/tratamento da informação relativa aos ficheiros disponiveis para transferência

def pedir_lista_de_ficheiros(ser):
    global pedido_id
    global listaficheiros
    pedido_id += 1
    log1(f"📨 Consulta da listagem dos ficheiros disponiveis no servidor. (ID Pedido = {pedido_id})")
    payload_aplicacao = criar_payload_aplicacao("".encode('utf-8'),0, ord('I'))

    if not confirm_pacote(ser, payload_aplicacao, pedido_id, 0, 0):
        log1(f"Não existe conexão com o servidor. (ID Pedido = {pedido_id})")
        return

    size = receber_tamanho(pedido_id)
    #print(size)
    npacotes = math.ceil(size / TAMANHO_MAX)
    dados = construir_dados(npacotes, pedido_id)

    if dados:
        ficheiros = dados.decode('utf-8').split('\\')
        listaficheiros.clear()
        for ficheiro in ficheiros:
            nome, tamanho = ficheiro.split(',')
            listaficheiros[nome] = int(tamanho)
    else:
        log1("⚠️ Nenhum dadoo recebido para a listagem de ficheiros." )



################ Consultar ficheiro dos voos

def ficheiro_voo():
    global pedido_id
    pedido_id += 1
    log1(f"📨 Consulta das informações sobre os voos. (ID Pedido = {pedido_id})")
    payload_aplicacao = criar_payload_aplicacao("".encode('utf-8'),0, ord('C'))

    if not confirm_pacote(ser, payload_aplicacao, pedido_id, 0, 0):
        log1(f"Não existe conexão com o servidor. (ID Pedido = {pedido_id})")
        return

    size = receber_tamanho(pedido_id)
    #print(size)
    npacotes = math.ceil(size / TAMANHO_MAX)
    dados = construir_dados(npacotes, pedido_id)
    data = []
    if dados:
        lines = dados.decode('utf-8').split("\n")
        colunas = lines[0].split(",")
        rows = lines[1:]
        #print(lines)
        for line in rows:
            if line:
                #print(line)
                columns = line.split(",")
                #print(columns[0])
                print(columns)
                data.append((columns[0],columns[1],columns[2],columns[3],columns[4],columns[5],columns[6],columns[7]))
        return data
    else:
        log1("⚠️ Nenhum dadoo recebido para a listagem de ficheiros." )
        return



################ Consultar a hora local do Aeroporto

def hora_local():
    global pedido_id
    pedido_id += 1
    payload_aplicacao = criar_payload_aplicacao("".encode('utf-8'),0, ord('T'))
    log1(f"📨 Consulta da hora local do aeroporto. (ID Pedido = {pedido_id})")

    if not confirm_pacote(ser, payload_aplicacao, pedido_id, 0, 0):
        log1(f"Não existe conexão com o servidor. (ID Pedido = {pedido_id})")
        return

    data = []
    while True:
        if not pacotes_buffer:
            time.sleep(0.1)
            continue
        for pacote in list(pacotes_buffer):
            id_pedido, seq, payload = pacote
            if id_pedido == pedido_id:
                log(f"📥 Pacote recebido ID_Pedido={id_pedido}, ID_Pacote={0}, SEQ={seq}")
                npacote , res = dividir_pacote_aplicacao(payload) 
                tempo = res.decode('utf-8')
                pacotes_buffer.remove(pacote)
                enviar_ack_nack(ser, 'A', id_pedido, seq)
                log("")
                return tempo
                
####################################################
#################### INTERFACE #####################
####################################################

# Logs 

# Troca de mensagens
def log(text):
    log_box.configure(state='normal')
    log_box.insert(tk.END, text + "\n")
    log_box.configure(state='disabled')
    log_box.see(tk.END)

# Registo de atividades
def log1(text):
    log1_box.configure(state='normal')
    log1_box.insert(tk.END, text + "\n")
    log1_box.configure(state='disabled')
    log1_box.see(tk.END)

# Abrir um ficheiro após a transferência
def mostrar_popup_e_abrir_ficheiro(caminho_ficheiro):
    nome = os.path.basename(caminho_ficheiro)
    messagebox.showinfo("Download Concluído", f"Ficheiro '{nome}' transferido com sucesso!")
    try:
        if sys.platform == "win32":
            os.startfile(caminho_ficheiro)
        elif sys.platform == "darwin":
            subprocess.call(["open", caminho_ficheiro])
        else:
            subprocess.call(["xdg-open", caminho_ficheiro])
    except Exception as e:
        messagebox.showerror("Erro ao abrir ficheiro", f"Não foi possível abrir o ficheiro.\n\n{e}")

#Interface
def start_gui(ser):

    # Pedir lista dos ficheiros
    def solicitar_info(tree):
        global listaficheiros
        pedir_lista_de_ficheiros(ser)
        tree.delete(*tree.get_children())
        for nome, tamanho in listaficheiros.items():
            tree.insert('', 'end', values=(nome, tamanho))

    # Transferir um ficheiro
    def solicitar_ficheiro():

        #Nome do ficheiro colocado na interface
        nome_ficheiro = file_entry.get()
        if nome_ficheiro in listaficheiros and listaficheiros[nome_ficheiro]<64000:
            transferir_ficheiro(nome_ficheiro,ser)
        elif listaficheiros == {}:
            log1("É necessário pedir a listagem dos ficheiros disponíveis para transferência.")
        elif listaficheiros[nome_ficheiro] >= 64000:
            log1("Ficheiro muito pesado.")
        else:
            log1("O ficheiro não está disponível para transferência.")

    #Atualizar a arvore que está na consulta dos voos
    def update_tree(columns, headings, data, flight_tree, active_view):
        active_view['columns'] = columns
        active_view['headings'] = headings

        flight_tree.config(columns=columns)
        for col in columns:
            flight_tree.heading(col, text=headings[col])
            flight_tree.delete(*flight_tree.get_children())
        for row in data:
            values = [row[i] for i in columns]
            flight_tree.insert('', 'end', values=values)

    #Pedir todas as informações no ficheiro
    def query1(flight_tree, active_view):
        global dummy_data
        dummy_data = ficheiro_voo()
        #Atualizar a arvore da interface
        update_tree((0, 5, 2, 3, 4, 1, 6, 7), { 0: "Destino", 5: "Embarque", 2: "Classe", 3: "Preco (eur)", 4: "Duração", 1: "Regiao", 6: "Porta", 7: "Companhia"}, dummy_data, flight_tree, active_view)

    #Pedir todos os voos para um destino
    def query2(flight_tree, active_view, voo_entry):
        global dummy_data
        Voo = voo_entry.get()
        dummy_data = ficheiro_voo()

        new_data = []

        #Filtrar para apresentar apenas o voo que pretendemos
        for (destino,b,c,d,e,f,g,h) in dummy_data:
            #print(destino)
            if destino == Voo:
                new_data.append((destino,b,c,d,e,f,g,h))

        dummy_data = new_data

        #Atualizar a arvore da interface
        update_tree((0, 5, 2, 3, 4, 6, 7), {0: "Destino", 5: "Embarque", 2: "Classe", 3: "Preco (eur)", 4: "Duração", 6: "Porta", 7: "Companhia"}, dummy_data, flight_tree, active_view)

    #Pedir os horarios dos voos num intervalo
    def query3(tempo1, tempo2, flight_tree, active_view):
        global dummy_data
        dummy_data = ficheiro_voo()
        new_data = []

        fmt = "%H:%M"
        hora1 = datetime.strptime(tempo1.get(), fmt)
        hora2 = datetime.strptime(tempo2.get(), fmt)

        #Filtrar para apresentar apenas os voos naquele intervalo de tempo
        for (a,b,c,d,e,embarque,g,h) in dummy_data:
            hora = datetime.strptime(embarque, fmt)
            if hora1 <= hora <= hora2:
                new_data.append((a,b,c,d,e,embarque,g,h))

        dummy_data = new_data

        update_tree((5, 0, 6, 7), {5:"Embarque", 0: "Destino",6:"Porta", 7: "Companhia"}, dummy_data, flight_tree, active_view)
        
    #Pedir a hora local do dispositivo e do Aeroporto/servidor
    def query4(time_label,time_label2):

        tempo = hora_local()
        update_time(time_label, [])
        update_time(time_label2, tempo)

    #Atualizar o tempo na interface
    def update_time(time_label, flag):
        fmt = "%H:%M:%S"
        #Hora do Aeroporto
        if flag:
            now = datetime.strptime(flag, fmt).time()
            time_label.config(text=f"Hora Local (Porto): {now}")
        #Hora local do Cliente
        else:
            now = datetime.now().strftime(fmt)
            time_label.config(text=f"Hora Atual: {now}")

    def exit_app(root):
        log1("A sair...")
        root.quit()

    # Nova janela com a consulta das informações nos voos
    def consult_info():
        flight_window = tk.Toplevel(root)
        flight_window.title("Informações dos Voos")
        flight_window.geometry("1000x500")

        search_frame = ttk.Frame(flight_window)
        search_frame.pack(padx=10, pady=5, fill='x')

        button_row2 = ttk.Frame(flight_window) 
        button_row2.pack(padx=10, pady=2, fill='x')

        button_row2a = ttk.Frame(flight_window)  
        button_row2a.pack(padx=10, pady=2, fill='x')

        button_row3a = ttk.Frame(flight_window)  
        button_row3a.pack(padx=10, pady=2, fill='x')

        button_row4 = ttk.Frame(flight_window)  
        button_row4.pack(padx=10, pady=2, fill='x')

        tree_frame = ttk.Frame(flight_window)
        tree_frame.pack(fill='both', expand=True)

        
        flight_tree = ttk.Treeview(tree_frame, show='headings')
        flight_tree.pack(fill='both', expand=True, padx=10, pady=10)

        active_view = {
            'columns': (0, 1, 2, 3, 4, 5, 6, 7),
            'headings': {
                0: "Destino", 1: "Regiao", 2: "Classe", 3: "Preco (eur)",
                4: "Duração", 5: "Embarque", 6: "Porta", 7: "Companhia"
            }
        }

        def perform_search():
            query = search_entry.get().lower()
            filtered = []
            for voo in dummy_data:
                for col_index in active_view['columns']:
                    if query in str(voo[col_index]).lower():
                        filtered.append(voo)
                        break
            update_tree(active_view['columns'], active_view['headings'], filtered, flight_tree, active_view)

        # Linha 1 - Filtrar a informação na interface
        ttk.Label(search_frame, text="Filtrar:").pack(side='left')
        search_entry = ttk.Entry(search_frame, width=20)
        search_entry.pack(side='left', padx=5)
        search_button = ttk.Button(search_frame, text="Filtrar", command=perform_search)
        search_button.pack(side='left', padx=5)
        reset_button = ttk.Button(search_frame, text="Reset filtragem", command=lambda: update_tree(active_view['columns'], active_view['headings'], dummy_data, flight_tree, active_view))
        reset_button.pack(side='left', padx=5)

        # Linha 2 - Toda a informação disponível sobre os voos
        query1_button = ttk.Button(button_row2, text="Toda a informação disponível sobre os voos", command=lambda: threading.Thread(target=query1, args=(flight_tree, active_view), daemon=True).start())
        query1_button.pack(side='left', padx=5)

        # Linha 3 - Todos os voos para um destino
        ttk.Label(button_row2a, text="Destino:").pack(side='left')
        Voo1_entry = ttk.Entry(button_row2a, width=10, justify='center')
        Voo1_entry.pack(side='left', padx=5)
        query2_button = ttk.Button(button_row2a, text="Todos os voos para um Destino", command=lambda: threading.Thread(target=query2, args=(flight_tree, active_view, Voo1_entry), daemon=True).start())
        query2_button.pack(side='left', padx=5)

        # Linha 4 - Horario dos voos num intervalo
        ttk.Label(button_row3a, text="Intervalo:").pack(side='left')
        Time1_entry = ttk.Entry(button_row3a, width=10, justify='center')
        Time1_entry.pack(side='left', padx=5)
        ttk.Label(button_row3a, text="\\").pack(side='left')

        Time2_entry = ttk.Entry(button_row3a, width=10, justify='center')
        Time2_entry.pack(side='left', padx=5)

        query3_button = ttk.Button(button_row3a, text="Horário dos voos", command=lambda: threading.Thread(target=query3, args=(Time1_entry,Time2_entry,flight_tree, active_view), daemon=True).start())
        query3_button.pack(side='left', padx=5)

        # Linha 5 - Tempo
        time_label = ttk.Label(button_row4, text="Hora Atual: --:--:--")
        time_label.pack(side='left', padx=5)

        time_label2 = ttk.Label(button_row4, text="Hora Local (Porto): --:--:--")
        time_label2.pack(side='left', padx=5)

        query4_button = ttk.Button(button_row4, text="Horário local", command=lambda: threading.Thread(target=query4, args=(time_label,time_label2,), daemon=True).start())
        query4_button.pack(side='left', padx=5)

    # Para quando se clicar num ficheiro
    def on_tree_select(event):
        selected = tree.selection()
        if selected:
            item = tree.item(selected[0])
            values = item.get('values', [])
            if values:
                file_entry.delete(0, tk.END)
                file_entry.insert(0, values[0])  # Nome do ficheiro

    root = tk.Tk()
    root.title("Interface do Cliente")

    
    tree = ttk.Treeview(root, columns=("name", "size"), show='headings')
    tree.heading("name", text="Nome do Ficheiro")
    tree.heading("size", text="Tamanho (bytes)")
    tree.pack(padx=10, pady=5, fill=tk.BOTH, expand=True)
    tree.bind("<<TreeviewSelect>>", on_tree_select)

    frame = ttk.LabelFrame(root, text="Transferências")
    frame.pack(padx=10, pady=5, fill='x')

    file_entry = ttk.Entry(frame)
    file_entry.pack(fill='x', padx=5)

    btn_frame = ttk.Frame(root)
    btn_frame.pack(pady=5)

    ttk.Button(btn_frame, text="Ficheiros disponíveis para transferência", command=lambda: threading.Thread(target=solicitar_info, args=(tree,), daemon=True).start()).pack(side='left', padx=5)
    ttk.Button(btn_frame, text="Transferir ficheiro (< 64KB)", command=lambda: threading.Thread(target=solicitar_ficheiro, args=(), daemon=True).start()).pack(side='left', padx=5)
    ttk.Button(btn_frame, text="Consultar informações dos voos", command=lambda: threading.Thread(target=consult_info, args=(), daemon=True).start()).pack(side='left', padx=5)

    # Registo das Atividades
    global log1_box
    log1_frame = ttk.LabelFrame(root, text="Registo das Atividades")
    log1_frame.pack(padx=10, pady=5, fill='both', expand=True)

    log1_box = tk.Text(log1_frame, height=7, state='disabled')
    log1_box.pack(fill='both', expand=True)

    # Troca de Mensagens
    global log_box
    log_frame = ttk.LabelFrame(root, text="Troca de mensagens")
    log_frame.pack(padx=10, pady=5, fill='both', expand=True)

    log_box = tk.Text(log_frame, height=7, state='disabled')
    log_box.pack(fill='both', expand=True)

    exit_f = ttk.Frame(root) 
    exit_f.pack(padx=10, pady=2, fill='x') 

    ttk.Button(exit_f, text="Sair", command=lambda: exit_app(root)).pack(fill='x', pady=5)

    root.mainloop()

####################################################
################ Data Receiver #####################
####################################################

# Guardar os acks recebidos num buffer e os dados noutro
def receber_dados(ser):
    while True:
        cabecalho = ler_bytes(ser, 5)
        if len(cabecalho) < 5:
            continue
        start, tipo, id_pedido, seq, tamanho = struct.unpack('<BBBBB', cabecalho)

        if tipo == ord('A') or tipo == ord('N'):
            ack_buffer.append((tipo, id_pedido, seq))
            #print(f"📥 ACK/NACK recebido ID={id_pedido}, SEQ={seq}")
            continue

        corpo = ler_bytes(ser, tamanho + 2)

        payload = corpo[:-2]
        crc_recebido = struct.unpack('<H', corpo[-2:])[0]
        crc_calculado = crc16(payload)

        #Erro payload tamanho errado
        #if len(corpo) < tamanho + 2:
        #    continue

        #Erro start bit
        if start != 0x7E:
            enviar_ack_nack(ser, 'N', id_pedido, seq)
            print("Erro startbit.")
            continue

        #Erro na seq
        if seq not in {0,1}:
            enviar_ack_nack(ser, 'N', id_pedido, seq)
            print("Erro na sequência.")
            continue

        #Erro no crc
        if crc_recebido != crc_calculado:
            print(f"❌ CRC errado para pacote ID={id_pedido}, SEQ={seq}")
            enviar_ack_nack(ser, 'N', id_pedido, seq)
            continue

        if tipo == ord('D'):
            pacotes_buffer.append((id_pedido, seq, payload))


################ Main ###############
if __name__ == '__main__':
    with serial.Serial(PORTA_SERIAL, BAUD_RATE, timeout=2) as ser:
        #Thread para receber acks e dados do servidor
        threading.Thread(target=receber_dados, args=(ser,), daemon=True).start()
        #Interface
        start_gui(ser)

