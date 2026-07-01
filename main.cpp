#include <Arduino.h>
#include <Servo.h>

const byte SERVO_PIN = 9;
Servo myServo;

// Cảm biến siêu âm
const byte trig_PIN = 5;
const byte echoPIN = 6;

long duration;
int distance;

// Đọc khoảng cách
int readDistance() {
    digitalWrite(trig_PIN, LOW);
    delayMicroseconds(2);

    digitalWrite(trig_PIN, HIGH);
    delayMicroseconds(10);
    digitalWrite(trig_PIN, LOW);

    duration = pulseIn(echoPIN, HIGH);

    distance = duration * 0.034 / 2;

    return distance;
}

void setup() {
    Serial.begin(9600);

    pinMode(trig_PIN, OUTPUT);
    pinMode(echoPIN, INPUT);

    myServo.attach(SERVO_PIN);
}

void loop() {

    // Quét từ 0 -> 180
    for (int angle = 0; angle <= 180; angle++) {

        myServo.write(angle);
        delay(20);                 // Chờ servo đến vị trí

        int distance = readDistance();

        Serial.print(angle);
        Serial.print(",");
        Serial.println(distance);
    }

    // Quét từ 180 -> 0
    for (int angle = 180; angle >= 0; angle--) {

        myServo.write(angle);
        delay(20);

        int distance = readDistance();

        Serial.print(angle);
        Serial.print(",");
        Serial.println(distance);
    }
}