//
// ICM-42688-P IMU as AP_InertialSensor_Invensensev3 drives it. The
// model supplies banked register storage, the 0x47 product ID, and
// 16-byte little-endian FIFO records at the programmed output data rate.
// Acceleration and angular-rate
// samples follow physics truth after applying the board's sensor rotation;
// temperature remains constant at 25C.
//
using System;
using System.Collections.Generic;
using Antmicro.Renode.Core;
using Antmicro.Renode.Peripherals;
using Antmicro.Renode.Peripherals.GPIOPort;
using Antmicro.Renode.Peripherals.SPI;
using Antmicro.Renode.Peripherals.Miscellaneous;
using Antmicro.Renode.Peripherals.Timers;
using Antmicro.Renode.Time;

namespace Antmicro.Renode.Peripherals.Sensors
{
    // IGPIOReceiver provides chip-select transaction framing in addition
    // to the H7 SPI model's TSIZE completion. PC15 deassertion also resets
    // the parser after an aborted or endless-mode transfer.
    public class AP_ICM42688 : ISPIPeripheral, IGPIOReceiver
    {
        public AP_ICM42688(IMachine machine, byte whoAmI = DefaultWhoAmI,
            byte rotation = 8, int samplePeriodUs = SamplePeriodUs,
            uint startupSampleCount = 0)
        {
            this.whoAmI = whoAmI;
            this.rotation = rotation;
            this.initialSamplePeriodUs = (ulong)Math.Max(1, samplePeriodUs);
            this.startupSampleCount = startupSampleCount;
            IRQ = new GPIO();
            physics = AP_PhysicsState.ForMachine(machine);
            fifo = new Queue<byte>();
            registers = new byte[BankCount, RegisterCount];
            sampleTimer = new LimitTimer(machine.ClockSource, 1000000, this, "icm42688 odr",
                                         limit: initialSamplePeriodUs, direction: Direction.Ascending,
                                         enabled: true, workMode: WorkMode.Periodic, eventEnabled: true);
            sampleTimer.LimitReached += OnSampleTick;
            Reset();
        }

        public void Reset()
        {
            Array.Clear(registers, 0, registers.Length);
            fifo.Clear();
            transferByte = 0;
            currentBank = 0;
            currentRegister = 0;
            reading = false;
            timestamp = 0;
            poweredSampleCount = 0;
            requestedSamplePeriodUs = initialSamplePeriodUs;
            sampleTimer.Limit = initialSamplePeriodUs;
            sampleTimer.ResetValue();
            IRQ.Unset();
            registers[0, WhoAmI] = whoAmI;
            registers[0, Icm45686WhoAmI] = whoAmI;
        }

        public byte Transmit(byte value)
        {
            byte response = 0;
            if(transferByte == 0)
            {
                reading = (value & ReadFlag) != 0;
                currentRegister = (byte)(value & RegisterMask);
            }
            else if(reading)
            {
                response = ReadRegister(currentRegister);
                if(currentRegister != FifoData)
                {
                    currentRegister = (byte)((currentRegister + 1) & RegisterMask);
                }
            }
            else
            {
                WriteRegister(currentRegister, value);
                if(currentRegister != FifoData)
                {
                    currentRegister = (byte)((currentRegister + 1) & RegisterMask);
                }
            }
            transferByte++;
            return response;
        }

        public void FinishTransmission()
        {
            transferByte = 0;
        }

        public void OnGPIO(int number, bool value)
        {
            if(value)
            {
                transferByte = 0;
            }
        }

        private byte ReadRegister(byte register)
        {
            if(currentBank == 0)
            {
                switch(register)
                {
                case FifoCountLow:
                    return (byte)(fifo.Count / CurrentSampleSize);
                case FifoCountHigh:
                    return (byte)((fifo.Count / CurrentSampleSize) >> 8);
                case FifoData:
                    return fifo.Count > 0 ? fifo.Dequeue() : (byte)0;
                case InterruptStatus:
                    return fifo.Count >= SampleSize ? DataReady : (byte)0;
                case BankSelect:
                    return currentBank;
                }
            }
            return registers[currentBank, register];
        }

        private void WriteRegister(byte register, byte value)
        {
            if(register == BankSelect)
            {
                var bank = (byte)(value & BankMask);
                currentBank = bank < BankCount ? bank : (byte)0;
                return;
            }

            if(currentBank == 0 && register == SignalPathReset && (value & FifoFlush) != 0)
            {
                fifo.Clear();
                registers[0, register] = 0;
                return;
            }
            if(currentBank == 0 && register == FifoData)
            {
                return;
            }
            registers[currentBank, register] = value;
            if(currentBank == 0 && register == GyroConfig0)
            {
                UpdateSamplePeriod(value & OutputDataRateMask);
            }
        }

        private void UpdateSamplePeriod(int outputDataRate)
        {
            // ICM42688 GYRO_ODR encodings used by Betaflight and ArduPilot.
            // Keeping this clock event at the real sensor cadence also gives
            // a Cortex-M in WFI an exact scheduling boundary to wake on.
            switch(outputDataRate)
            {
            case 3: requestedSamplePeriodUs = 125; break;  // 8 kHz
            case 4: requestedSamplePeriodUs = 250; break;  // 4 kHz
            case 5: requestedSamplePeriodUs = 500; break;  // 2 kHz
            case 6: requestedSamplePeriodUs = 1000; break; // 1 kHz
            case 7: requestedSamplePeriodUs = 5000; break; // 200 Hz
            case 8: requestedSamplePeriodUs = 10000; break; // 100 Hz
            case 9: requestedSamplePeriodUs = 20000; break; // 50 Hz
            case 10: requestedSamplePeriodUs = 40000; break; // 25 Hz
            case 11: requestedSamplePeriodUs = 80000; break; // 12.5 Hz
            case 13: requestedSamplePeriodUs = 320000; break; // 3.125 Hz
            case 15: requestedSamplePeriodUs = 2000; break; // 500 Hz
            default: return;
            }
            if(poweredSampleCount >= startupSampleCount)
            {
                sampleTimer.Limit = requestedSamplePeriodUs;
            }
        }

        private void OnSampleTick()
        {
            var sampleSize = CurrentSampleSize;
            if((registers[0, PowerManagement] & SensorsLowNoise) != SensorsLowNoise)
            {
                return;
            }
            poweredSampleCount++;
            if(poweredSampleCount == startupSampleCount &&
               sampleTimer.Limit != requestedSamplePeriodUs)
            {
                // Betaflight validates a SPI gyro by counting 1000 native-rate
                // data-ready interrupts before selecting interrupt/DMA mode.
                // Its ESCSim profile then requests 200Hz, so preserve the
                // model's 8kHz reset cadence only for that validation window.
                sampleTimer.Limit = requestedSamplePeriodUs;
            }
            // SPEEDYBEEF405V5 wires the gyro data-ready signal to PC4/EXTI4.
            // Pulsing it at the programmed ODR mirrors the hardware and wakes
            // the sequence-patched scheduler exactly at its gyro boundary.
            IRQ.Blink();
            if((registers[0, FifoConfig1] & FifoSensorsEnabled) != FifoSensorsEnabled ||
               fifo.Count + sampleSize > FifoCapacity)
            {
                return;
            }

            if(HighResolutionEnabled)
            {
                PushHighResolutionSample();
                return;
            }

            fifo.Enqueue(FifoHeader);
            var truth = physics.Current;
            var acceleration = AP_SensorOrientation.BodyToSensor(truth.SpecificForceMS2, rotation);
            var gyro = AP_SensorOrientation.BodyToSensor(truth.GyroRadS, rotation);
            PushWord(ScaleWord(acceleration[0], AccelScale));
            PushWord(ScaleWord(acceleration[1], AccelScale));
            PushWord(ScaleWord(acceleration[2], AccelScale));
            PushWord(ScaleWord(gyro[0], GyroScale));
            PushWord(ScaleWord(gyro[1], GyroScale));
            PushWord(ScaleWord(gyro[2], GyroScale));
            fifo.Enqueue(0);
            PushWord((short)timestamp++);
        }

        private void PushHighResolutionSample()
        {
            fifo.Enqueue(FifoHighResolutionHeader);
            var truth = physics.Current;
            var acceleration = AP_SensorOrientation.BodyToSensor(truth.SpecificForceMS2, rotation);
            var gyro = AP_SensorOrientation.BodyToSensor(truth.GyroRadS, rotation);
            var values = new int[] {
                ScaleHighResolution(acceleration[0], AccelHighResolutionScale),
                ScaleHighResolution(acceleration[1], AccelHighResolutionScale),
                ScaleHighResolution(acceleration[2], AccelHighResolutionScale),
                ScaleHighResolution(gyro[0], GyroHighResolutionScale),
                ScaleHighResolution(gyro[1], GyroHighResolutionScale),
                ScaleHighResolution(gyro[2], GyroHighResolutionScale),
            };
            foreach(var value in values)
            {
                fifo.Enqueue((byte)(value >> 4));
                fifo.Enqueue((byte)(value >> 12));
            }
            PushWord(0);
            PushWord((short)timestamp++);
            fifo.Enqueue((byte)((values[3] & 0xF) | (values[0] & 0xF) << 4));
            fifo.Enqueue((byte)((values[4] & 0xF) | (values[1] & 0xF) << 4));
            fifo.Enqueue((byte)((values[5] & 0xF) | (values[2] & 0xF) << 4));
        }

        private static short ScaleWord(float value, double scale)
        {
            return (short)Math.Max(Int16.MinValue,
                Math.Min(Int16.MaxValue, Math.Round(value / scale)));
        }

        private static int ScaleHighResolution(float value, double scale)
        {
            return (int)Math.Max(-524288,
                Math.Min(524287, Math.Round(value / scale)));
        }

        private void PushWord(short value)
        {
            fifo.Enqueue((byte)(value & 0xFF));
            fifo.Enqueue((byte)((value >> 8) & 0xFF));
        }

        private bool HighResolutionEnabled =>
            (registers[0, FifoConfig1] & FifoHighResolutionEnable) != 0;
        private int CurrentSampleSize => HighResolutionEnabled ? HighResolutionSampleSize : SampleSize;

        private readonly Queue<byte> fifo;
        private readonly byte[,] registers;
        private readonly LimitTimer sampleTimer;
        private readonly byte whoAmI;
        private readonly byte rotation;
        private readonly ulong initialSamplePeriodUs;
        private readonly uint startupSampleCount;
        private readonly AP_PhysicsState physics;
        public GPIO IRQ { get; }
        private int transferByte;
        private byte currentBank;
        private byte currentRegister;
        private bool reading;
        private ushort timestamp;
        private uint poweredSampleCount;
        private ulong requestedSamplePeriodUs;

        private const int BankCount = 5;
        private const int RegisterCount = 128;
        private const int SamplePeriodUs = 1000;
        private const int SampleSize = 16;
        private const int HighResolutionSampleSize = 20;
        private const int FifoCapacity = 2048;

        private const byte SignalPathReset = 0x4B;
        private const byte PowerManagement = 0x4E;
        private const byte GyroConfig0 = 0x4F;
        private const byte FifoConfig1 = 0x5F;
        private const byte InterruptStatus = 0x2D;
        private const byte FifoCountLow = 0x2E;
        private const byte FifoCountHigh = 0x2F;
        private const byte FifoData = 0x30;
        private const byte WhoAmI = 0x75;
        private const byte Icm45686WhoAmI = 0x72;
        private const byte BankSelect = 0x76;

        private const byte ReadFlag = 0x80;
        private const byte RegisterMask = 0x7F;
        private const byte BankMask = 0x07;
        private const byte OutputDataRateMask = 0x0F;
        private const byte DefaultWhoAmI = 0x47;
        private const byte DataReady = 0x08;
        private const byte FifoFlush = 0x02;
        private const byte FifoSensorsEnabled = 0x07;
        private const byte FifoHighResolutionEnable = 0x10;
        private const byte SensorsLowNoise = 0x0F;
        private const byte FifoHeader = 0x68;
        private const byte FifoHighResolutionHeader = 0x78;
        private const double Gravity = 9.80665;
        private const double AccelScale = Gravity * 16.0 / 32768.0;
        private const double GyroScale = Math.PI / 180.0 * 2000.0 / 32768.0;
        private const double AccelHighResolutionScale = Gravity * 16.0 / 524288.0;
        private const double GyroHighResolutionScale = Math.PI / 180.0 * 2000.0 / 524288.0;
    }
}
