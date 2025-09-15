#Servidor
import os
import time
import struct
import serial
import crcmod
import sys
import threading
from collections import deque
from threading import Lock
from datetime import datetime, timedelta
import tkinter as tk
from tkinter import ttk
import random
#import Simuladordosvoos


sys.stdout.reconfigure(encoding='utf-8')

PORTA_SERIAL = 'COM6'
BAUD_RATE = 19200
TAMANHO_MAX = 245  # Max dados no payload
MAX_RETRIES = 3
TIMEOUT_ACK = 5
NPACOTES_PARA_ERRO = 4

ack_buffer = deque()  # (type, id, seq)
serial_write_lock = Lock()
ack_buffer_lock = threading.Lock()

#Pasta com os ficheiros
pasta = "ficheiros"
fsizes = []
ficheiro_para_consulta = ""
tempoffset = timedelta() 
flag_erros = False



####################################################
################ Camada de Ligação #################
####################################################

# CRC
crc16 = crcmod.predefined.mkCrcFun('crc-ccitt-false')


# Criar o pacote a ser enviado ligação + payload + crc
def criar_pacote_ligacao(payload_aplicacao, id_pedido, seq, erro_crc, erro_startbit):
    payload_len = len(payload_aplicacao)
    crc = crc16(payload_aplicacao)
    start = 0x7E

    if erro_crc:
        crc = 0x7E
    if erro_startbit:
        start = 0x7D

    header = struct.pack('<BBBBB', start, ord('D'), id_pedido, seq, payload_len)
    crc_bytes = struct.pack('<H', crc)
    return header + payload_aplicacao + crc_bytes

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
# Se a flag de erros tiver ativa, de X em X pacotes enviados (associados ao mesmo pedido) é enviado um com erros na sua primeira tentativa
def confirm_pacote(ser, payload_aplicacao, id_pedido, seq, id_pacote): 

    # Variável para assegurar que o pacote é enviado sem erros na segunda tentativa
    errar = True
    pacote = criar_pacote_ligacao(payload_aplicacao, id_pedido, seq, False, False)
    for tentativa in range(MAX_RETRIES):
        #-1 Porque o id_pacote começa em 0
        if flag_erros and (id_pacote % (NPACOTES_PARA_ERRO-1)) == 0 and errar and id_pacote != 0:
            rand = random.randint(0, 2)
            if rand == 0:
                #Erro na sequencia
                pacote_erro = criar_pacote_ligacao(payload_aplicacao, id_pedido, 4, False, False)
            elif rand == 1:
                #Erro no startbit
                pacote_erro = criar_pacote_ligacao(payload_aplicacao, id_pedido, seq, False, True)
            else:
                #Erro no crc
                pacote_erro = criar_pacote_ligacao(payload_aplicacao, id_pedido, seq, True, False)

            errar = False
            with serial_write_lock:
                ser.write(pacote_erro)

        else:
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
            log(f"⏱️ Timeout à espera de um ACK/NACK (tentativa {tentativa + 1})")
    log("❌ Máximo de tentativas atingido")
    return False

#Enviar ack ou nack
def enviar_ack_nack(ser, tipo, id_pedido, seq):
    pacote = bytearray([0x7E, ord(tipo), id_pedido, seq, 0])
    with serial_write_lock:
        ser.write(pacote)
    log(f"📤 Enviado {'ACK' if tipo == 'A' else 'NACK'} -> ID={id_pedido}, SEQ={seq}")


#Ler X bytes do serial
def ler_bytes(ser, tamanho):
    dados = bytearray()
    while len(dados) < tamanho:
        parte = ser.read(tamanho - len(dados))
        if not parte:
            break
        dados.extend(parte)
    return dados

#Receção de pacote e analise do tipo na camada de ligação
#Se A ou N (Ack/Nack) é adicionado num buffer
#Se D cria thread para tratar do pedido na camada de aplicação
def receber_pacote():
    with serial.Serial(PORTA_SERIAL, BAUD_RATE, timeout=2) as ser:
        print(f"🔌 Ouvir {PORTA_SERIAL} @ {BAUD_RATE} baud")
        while True:
            header = ler_bytes(ser, 5)
            if len(header) < 5:
                continue

            start, tipo, id_pedido, seq, tamanho = struct.unpack('<BBBBB', header)
            if start != 0x7E:
                continue

            if tipo in (ord('A'), ord('N')):
                with ack_buffer_lock:
                    ack_buffer.append((tipo, id_pedido, seq))
                #log(ack_buffer)
                continue

            corpo_crc = ler_bytes(ser, tamanho + 2)
            if len(corpo_crc) < tamanho + 2:
                continue

            payload = corpo_crc[:tamanho]
            crc_recebido = struct.unpack('<H', corpo_crc[-2:])[0]

            if crc16(payload) != crc_recebido:
                enviar_ack_nack(ser, 'N', id_pedido, seq)
                print("❌ CRC inválido")
                continue

            if tipo == ord('D'):
                threading.Thread(target=processar_pedido, args=(ser, payload, seq, id_pedido, ), daemon=True).start()


####################################################
################ Camada de Aplicação ###############
####################################################


#Verifica se o ficheiro existe
def procurar_arquivo(arquivo):
    caminho = os.path.join(pasta, arquivo)
    if not os.path.exists(caminho):
        log1(f"❌ Arquivo {arquivo} não encontrado!")
        return None
    return caminho

#Cria o pacote de aplicação Tipo + Id_ pacote + Tamanho + dados
def criar_payload_aplicacao(dados, id_pacote, tipo):
    #tipo = ord('D')
    tamanho_dados = len(dados)
    #log(tamanho_dados)
    return struct.pack('<BBB', tipo, id_pacote, tamanho_dados) + dados


#Lê o ficheiro, criasse o pacote da camada de aplicação e comunica com a camada de ligação para a questão de
#adicionar a camada de ligação ao pacote e de tratar os acks (Função confirm_pacote)
def gerar_resposta(nome_ficheiro, caminho_ficheiro, id_pedido, ser, tipo):
    with open(caminho_ficheiro, 'rb') as f:
        seq = 0
        id_pacote = 0
        while True:
            dados = f.read(TAMANHO_MAX)
            if not dados:
                break
            payload_aplicacao = criar_payload_aplicacao(dados, id_pacote, tipo)
            #Camada ligação
            if not confirm_pacote(ser, payload_aplicacao, id_pedido, seq, id_pacote):
                break
            seq ^= 1
            id_pacote += 1
            time.sleep(0.2)

    log("")
    log1(f"✅ Transmissão finalizada (ID Pedido = {id_pedido})")


# Para casos em que é enviado um texto para o cliente (Envia o tamanho do texto e depois o texto)
def gerar_resposta_texto(text,tipo,ser,id_pedido):
    seq = 0
    id_pacote = 0

    #Enviar tamanho do texto
    payload_aplicacao = criar_payload_aplicacao(str(len(text)).encode('utf-8'), id_pacote, tipo)

    if not confirm_pacote(ser, payload_aplicacao, id_pedido, seq, id_pacote):
        return

    seq ^= 1
    id_pacote += 1

    # Envio 
    i = 0
    while i < len(text):
        dados = text[i:i+TAMANHO_MAX]
        payload_aplicacao = criar_payload_aplicacao(dados, id_pacote, tipo)
        
        # Camada de ligação
        if not confirm_pacote(ser, payload_aplicacao, id_pedido, seq, id_pacote):
            break

        seq ^= 1
        id_pacote += 1
        i += TAMANHO_MAX
        time.sleep(0.2)

    log("")
    log1(f"✅ Transmissão finalizada (ID Pedido = {id_pedido})")


#Função principal da camada de aplicação
#Se o tipo for D, temos presentes um pedido para transferência de um ficheiro
#Se o tipo for I, temos presentes um pedido para listagem dos ficheiros disponiveis para transferência, no servidor
def processar_pedido(ser, payload, seq, id_pedido):
    tipo, id_pacote, tamanho = struct.unpack('<BBB', payload[:3])
    log(f"📥 Pacote recebido ID_Pedido={id_pedido}, ID_Pacote={id_pacote}, SEQ={seq}")
    enviar_ack_nack(ser, 'A', id_pedido, seq)

    if(tipo == ord('D')):
        nome_arquivo = payload[3:3 + tamanho].decode('utf-8')
        caminho = procurar_arquivo(nome_arquivo)
        log1(f"Pedido para transferência do ficheiro {nome_arquivo} (ID Pedido = {id_pedido})")
        if caminho:
            gerar_resposta(nome_arquivo, caminho, id_pedido, ser, tipo)

    # Nome,tamanho\Nome,tamanho\...
    if(tipo == ord('I')):
        log1(f"Pedido da listagem dos ficheiros disponiveis para transferência (ID Pedido = {id_pedido})")
        text = '\\'.join(f"{name},{size}" for name, size in fsizes)
        gerar_resposta_texto(text.encode('utf-8'),tipo,ser,id_pedido)

    if(tipo == ord('C')):
        log1(f"Pedido para a consulta do ficheiro com informações dos voos (ID Pedido = {id_pedido})")
        with open(ficheiro_para_consulta, "r", encoding="utf-8") as f:
            text = f.read()
            f.close()
        gerar_resposta_texto(text.encode('utf-8'),tipo,ser,id_pedido)

    if(tipo == ord('T')):
        log1(f"Pedido para a consulta o tempo local do Aeroporto (ID Pedido = {id_pedido})")
        text = tempo_atual()

        payload_aplicacao = criar_payload_aplicacao(text.encode('utf-8'), 0, tipo)
        #Camada de Ligação
        if not confirm_pacote(ser, payload_aplicacao, id_pedido, seq,0):
            return

        log("")
        log1(f"✅ Transmissão finalizada (ID Pedido = {id_pedido})")

def tempo_atual():
    return (datetime.now() + tempoffset).strftime("%H:%M:%S")

####################################################
#################### INTERFACE #####################
####################################################

# Listagem dos ficheiros disponiveis para transferência no servidor
def ficheiros_e_tamanho():
    current_dir = pasta
    files_info = []
    for entry in os.listdir(current_dir):
        full_path = os.path.join(current_dir, entry)
        if os.path.isfile(full_path):
            size = os.path.getsize(full_path)
            files_info.append((entry, size))
    
    return files_info

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

def launch_interface():

    def ativar_flag_erros():
        global flag_erros
        flag_erros = True
        log1(f"De {NPACOTES_PARA_ERRO} em {NPACOTES_PARA_ERRO} pacotes do mesmo pedido, é enviado um pacote com erros.")

    def desativar_flag_erros():
        global flag_erros
        flag_erros = False
        log1("Os pacotes enviados deixaram de ter erros.")

    def incrementar_hora():
        global tempoffset
        tempoffset += timedelta(hours=1)
        log1(f"A hora local do aeroporto foi atualizada para {tempo_atual()}.")

    def incrementar_minutos():
        global tempoffset
        tempoffset += timedelta(minutes=10)
        log1(f"A hora local do aeroporto foi atualizada para {tempo_atual()}.")

    def atualizar_lista_ficheiros():
        global fsizes
        fsizes = ficheiros_e_tamanho()
        log1("Lista dos ficheiros para transferência foi atualizada.")

    def ativar_simulador():
        global flag_ligado 
        flag_ligado = True
        log1("O simulador dos voos foi ativado.")
        comecar()

    def desativar_simulador():
        parar()
        log1("O simulador dos voos foi desativado.")

    def alterar_ficheiro(ficheiro_entry):
        global ficheiro_para_consulta
        nome_ficheiro = ficheiro_entry.get()
        if ".csv" in nome_ficheiro:
            ficheiro_para_consulta = nome_ficheiro
            log1(f"Ficheiro para consultar as informações dos voos foi alterado.({nome_ficheiro})")
        else:
            log1("Ficheiro tem de ser .csv .")


    def exit_app(root):
        log1("A sair...")
        root.quit()

    root = tk.Tk()
    root.title("Interface do Servidor")

    frame = ttk.Frame(root, padding=20)
    frame.pack(fill='both', expand=True)

    # Ajustar flag
    flag_frame = ttk.Frame(frame)
    flag_frame.pack(fill='x', pady=5)
    ttk.Button(flag_frame, text="Ativar flag para pacotes com erros", command=ativar_flag_erros).pack(side='left',fill="x", expand=True, padx=2)
    ttk.Button(flag_frame, text="Desativar flag para pacotes com erros", command=desativar_flag_erros).pack(side='left',fill="x", expand=True, padx=2)

    # Ajustar o tempo
    time_frame = ttk.Frame(frame)
    time_frame.pack(fill='x', pady=5)
    
    ttk.Button(time_frame, text="Incrementar 1 hora á hora local", command=incrementar_hora).pack(side='left',fill="x", expand=True, padx=2)
    ttk.Button(time_frame, text="Incrementar 10 minuto á hora local", command=incrementar_minutos).pack(side='left',fill="x", expand=True, padx=2)

    # Simulador de voos
    simulador_frame = ttk.Frame(frame)
    simulador_frame.pack(fill='x', pady=5)
    what = ttk.Button(simulador_frame, text="Ativar o simulador de voos", command=lambda: threading.Thread(target=ativar_simulador, args=(), daemon=True).start()).pack(side='left',fill="x", expand=True, padx=2)
    ttk.Button(simulador_frame, text="Desativar o simulador de voos", command=desativar_simulador).pack(side='left',fill="x", expand=True, padx=2)

    ttk.Button(frame, text="Atualizar a lista de ficheiros", command=atualizar_lista_ficheiros).pack(fill='x', pady=5)

    ttk.Button(frame, text="Alterar o ficheiro de consulta", command=lambda: alterar_ficheiro(ficheiro_entry)).pack( fill='x', pady=5)
    ttk.Label(frame, text="Ficheiro csv : ").pack(side='left',fill='x', padx=5)
    ficheiro_entry = ttk.Entry(frame, width=40, justify='center')
    ficheiro_entry.pack(side='left', fill='x', padx=5)

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
###################### Simulador ###################
####################################################

flag_ligado = True

#Adicionar a cada voo que ja partiu
minutos = 5

Atualizacao = 10 #Segundos

def atualizar_csv(text):
    formato = "%H:%M"
    hora2 = datetime.strptime(tempo_atual(), "%H:%M:%S").time()
    final = ""
    if text:
        lines = text.split("\n")
        rows = lines[1:]
        contador = 1
        
        final += lines[0] + "\n" 
        troca = ""
        mins = 0
        # Quando um voo ja ocorreu ele é colocado no fim
        for line in rows:
            if line.strip():
                columns = line.split(",")
                hora1 = datetime.strptime(columns[5].strip(), "%H:%M").time()   

                if hora1 < hora2:
                    mins += minutos
                    # Atualizar as horas da linha
                    tempo = datetime(2000, 1, 1)
                    columns[5] = ((datetime.combine(tempo, hora2) + timedelta(minutes=mins)).time()).strftime("%H:%M")
                        
                    final += ",".join(columns) + "\n"
                    print(f"A linha {contador} foi atualizada.")
                else:
                    troca += line + "\n"
                contador += 1
        
        final += troca
        final += line[random.randint(0,len(lines)-1)]
    else:
        print("O ficheiro não tem informações.")
        final = ""

    return final.strip()
        

#Ativar o simulador
def comecar():
    # O simulador de X em X tempo atualiza o csv que está a ser usado no servidor para a consulta das informações dos voos.
    while flag_ligado:
        #print(ficheiro_para_consulta)
        with open(ficheiro_para_consulta, "r", encoding="utf-8") as f:
            text = f.read()
            f.close()

        final = atualizar_csv(text)

        with open(ficheiro_para_consulta, "w", encoding="utf-8") as f:
            f.write(final)
            f.close()

        log1(f"Simulador atualizou o ficheiro {ficheiro_para_consulta} .")

        time.sleep(Atualizacao)

        
    print("Simulador foi desligado.")


#Desativar o simulador
def parar():
    global flag_ligado
    flag_ligado = False



####################################################
###################### MAIN ########################
####################################################

if __name__ == '__main__':
    fsizes = ficheiros_e_tamanho()
    ficheiro_para_consulta = "ficheiros\\Voos.csv"
    #log('\\'.join(f"{name},{size}" for name, size in fsizes))
    #Função principal do código
    threading.Thread(target= receber_pacote, args=(), daemon=True).start()
    #Interface
    launch_interface()

