#include <Arduino.h>

#define UART_RX 16     // UART2 RX 
#define UART_TX 17     // UART2 TX 
#define UART_BAUD 19200

HardwareSerial ReceiverUART(2);  // UART2

void setup() {
  Serial.begin(9600);  // USB 
  ReceiverUART.begin(UART_BAUD, SERIAL_8N1, UART_RX, UART_TX);  // UART2
}

void loop() {
  // UART2 -> USB
  while (ReceiverUART.available()) {
    uint8_t b = ReceiverUART.read();
    Serial.write(b);
    Serial.flush();            
  }

  // USB -> UART2
  while (Serial.available()) {
    uint8_t b = Serial.read();
    ReceiverUART.write(b);
    ReceiverUART.flush();      
  }
}
