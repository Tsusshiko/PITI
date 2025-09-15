#include <Arduino.h>


#define UART_TX 17  // UART2 TX
#define UART_RX 16  // UART2 RX
#define UART_BAUD 19200 

HardwareSerial SenderUART(2);  // UART2

void setup() {
  Serial.begin(9600); 
  SenderUART.begin(UART_BAUD, SERIAL_8N1, UART_RX, UART_TX);  // UART2
  
}

void loop() {
  // USB -> UART2
  if (Serial.available()) {
    uint8_t b = Serial.read();
    SenderUART.write(b);
    SenderUART.flush();      
  }

  // UART2 -> USB
  if (SenderUART.available()) {
    uint8_t b = SenderUART.read();
    Serial.write(b);
    Serial.flush();         
  }
}
