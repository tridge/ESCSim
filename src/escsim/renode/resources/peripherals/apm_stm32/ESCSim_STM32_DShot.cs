// Bridges the SpeedyBeeF405Mini timer DMA waveform to four independent
// ESCSim ESC signal sockets.  ArduPilot interleaves all four timer channels in
// one DMAR buffer; decoding it here preserves the actual packet, including
// commands and the inverted checksum used by bidirectional DShot.
//
// Bidirectional replies arrive from the ESC as ESCSim's GCR-decoded 16-bit
// frame.  They are converted back to timer input-capture edge timestamps and
// delivered through the same STM32 DMA streams firmware configured for the
// physical pins.  The reply cached from the preceding output cycle is used:
// separate Renode processes do not share virtual time, and this one-cycle
// pipeline keeps the FC's 80us capture window deterministic.
using System;
using System.Net;
using System.Net.Sockets;
using System.Threading;

using Antmicro.Renode.Core;
using Antmicro.Renode.Core.Structure.Registers;
using Antmicro.Renode.Peripherals;
using Antmicro.Renode.Peripherals.Bus;
using Antmicro.Renode.Peripherals.DMA;
using Antmicro.Renode.Peripherals.Timers;
using Antmicro.Renode.Time;

namespace Antmicro.Renode.Peripherals.Miscellaneous
{
    public sealed class ESCSim_STM32_DShot : IDoubleWordPeripheral, IKnownSize,
        IDisposable
    {
        public ESCSim_STM32_DShot(IMachine machine, STM32DMA dma,
            STM32_Timer timer3, STM32_Timer timer4,
            int esc1Port, int esc2Port, int esc3Port, int esc4Port)
        {
            this.machine = machine;
            this.dma = dma;
            this.timer3 = timer3;
            this.timer4 = timer4;
            sockets = new UdpClient[EscCount];
            cachedReplies = new ushort?[EscCount];
            var ports = new[] { esc1Port, esc2Port, esc3Port, esc4Port };
            for(var index = 0; index < EscCount; index++)
            {
                if(ports[index] <= 0)
                {
                    continue;
                }
                var socket = new UdpClient(new IPEndPoint(IPAddress.Loopback, 0));
                socket.Connect(IPAddress.Loopback, ports[index]);
                socket.Client.ReceiveTimeout = ReceiveTimeoutMs;
                sockets[index] = socket;
                var captured = index;
                var thread = new Thread(() => Receive(captured))
                {
                    IsBackground = true,
                    Name = string.Format("ESCSim ESC{0} reply", index + 1),
                };
                thread.Start();
            }

            dma.RegistersCollection.AddBeforeWriteHook(
                StreamConfiguration(OutputTimer3Stream),
                (offset, value) => ObserveTimer3(value));
            dma.RegistersCollection.AddBeforeWriteHook(
                StreamConfiguration(OutputTimer4Stream),
                (offset, value) => ObserveDma(
                    OutputTimer4Stream, timer4, Timer4Escs, value));
            dma.RegistersCollection.AddBeforeWriteHook(
                StreamConfiguration(CaptureTimer4Stream),
                (offset, value) => ObserveCapture(timer4,
                    CaptureTimer4Stream, value));
        }

        public void Reset()
        {
            lock(sync)
            {
                Array.Clear(cachedReplies, 0, cachedReplies.Length);
            }
            FramesSent = 0;
            RepliesReceived = 0;
            RepliesInjected = 0;
        }

        public uint ReadDoubleWord(long offset)
        {
            switch(offset)
            {
            case 0x00: return FramesSent;
            case 0x04: return RepliesReceived;
            case 0x08: return RepliesInjected;
            default: return 0;
            }
        }

        public void WriteDoubleWord(long offset, uint value) { }

        public long Size => 0x100;

        public uint FramesSent { get; private set; }
        public uint RepliesReceived { get; private set; }
        public uint RepliesInjected { get; private set; }

        public void Dispose()
        {
            disposed = true;
            foreach(var socket in sockets)
            {
                if(socket != null)
                {
                    socket.Close();
                }
            }
        }

        private uint? ObserveTimer3(uint configuration)
        {
            ObserveDma(OutputTimer3Stream, timer3, Timer3Escs, configuration);
            ObserveCapture(timer3, CaptureTimer3Stream, configuration);
            return null;
        }

        private uint? ObserveCapture(STM32_Timer timer, int stream,
            uint configuration)
        {
            if((configuration & StreamEnable) == 0 ||
               (configuration & DirectionMask) != PeripheralToMemory ||
               dma.ReadDoubleWord(StreamPeripheral(stream)) !=
                   TimerDmarAddress(timer))
            {
                return null;
            }
            return Schedule(() => Inject(timer, stream));
        }

        private uint? Schedule(Action action)
        {
            machine.ScheduleAction(TimeInterval.FromMicroseconds(1), _ => action(),
                name: "ESCSim DShot DMA observation");
            return null;
        }

        private uint? ObserveDma(int stream, STM32_Timer timer, int[] escs,
            uint configuration)
        {
            if((configuration & StreamEnable) == 0 ||
               (configuration & DirectionMask) != MemoryToPeripheral)
            {
                return null;
            }
            var peripheral = dma.ReadDoubleWord(StreamPeripheral(stream));
            if(peripheral != TimerDmarAddress(timer))
            {
                return null;
            }
            var count = (int)dma.ReadDoubleWord(StreamCount(stream));
            if(count < DshotWords)
            {
                return null;
            }
            var memory = dma.ReadDoubleWord(StreamMemory(stream));
            var autoReload = timer.ReadDoubleWord(AutoReload);
            for(var channel = 0; channel < ChannelsPerTimer; channel++)
            {
                var esc = escs[channel];
                if(esc < 0 || !TryDecode(memory, channel, autoReload,
                    out var frame))
                {
                    continue;
                }
                var bidirectional = IsBidirectional(frame);
                SendFrame(esc, frame, autoReload, bidirectional);
                FramesSent++;
            }
            return null;
        }

        private bool TryDecode(uint memory, int channel, uint autoReload,
            out ushort frame)
        {
            frame = 0;
            var any = false;
            for(var bit = 0; bit < DshotBits; bit++)
            {
                var word = machine.SystemBus.ReadDoubleWord(
                    memory + (uint)((DshotPreamble + bit) * ChannelsPerTimer +
                        channel) * sizeof(uint));
                frame <<= 1;
                if(word > autoReload / 2)
                {
                    frame |= 1;
                }
                any |= word != 0;
            }
            return any && (HasNormalChecksum(frame) || IsBidirectional(frame));
        }

        private void SendFrame(int esc, ushort frame, uint autoReload,
            bool bidirectional)
        {
            var packet = new byte[8];
            Put16(packet, 0, Magic);
            packet[2] = DshotType(autoReload);
            packet[3] = 4;
            Put16(packet, 4, bidirectional ? IdleHigh : (ushort)0);
            Put16(packet, 6, frame);
            try
            {
                var socket = sockets[esc];
                if(socket != null)
                {
                    socket.Send(packet, packet.Length);
                }
            }
            catch(SocketException)
            {
                // An ESC may still be starting or resetting.  DShot itself is
                // lossy, so the next output frame is the correct retry.
            }
        }

        private void Receive(int esc)
        {
            var endpoint = new IPEndPoint(IPAddress.Loopback, 0);
            while(!disposed)
            {
                try
                {
                    var packet = sockets[esc].Receive(ref endpoint);
                    if(packet.Length < 8 || Get16(packet, 0) != Magic ||
                       packet[3] != 4 || packet[2] > Dshot600)
                    {
                        continue;
                    }
                    lock(sync)
                    {
                        cachedReplies[esc] = Get16(packet, 6);
                    }
                    RepliesReceived++;
                }
                catch(SocketException exception)
                {
                    if(exception.SocketErrorCode != SocketError.TimedOut &&
                       !disposed)
                    {
                        return;
                    }
                }
                catch(ObjectDisposedException)
                {
                    return;
                }
            }
        }

        private void Inject(STM32_Timer timer, int stream)
        {
            var configuration = dma.ReadDoubleWord(StreamConfiguration(stream));
            if((configuration & StreamEnable) == 0 ||
               (configuration & DirectionMask) != PeripheralToMemory)
            {
                return;
            }
            var esc = EscForCapture(timer);
            if(esc < 0)
            {
                return;
            }
            ushort? reply;
            lock(sync)
            {
                reply = cachedReplies[esc];
                cachedReplies[esc] = null;
            }
            if(!reply.HasValue)
            {
                return;
            }

            var gcr = EncodeGcr(reply.Value);
            var memory = dma.ReadDoubleWord(StreamMemory(stream));
            var remaining = dma.ReadDoubleWord(StreamCount(stream));
            if(remaining == 0)
            {
                return;
            }
            var used = 0u;
            uint timestamp = CaptureTick;
            machine.SystemBus.WriteDoubleWord(memory, timestamp);
            used++;
            for(var bit = GcrBits - 1; bit >= 0; bit--)
            {
                timestamp += CaptureTick;
                if((gcr & (1u << bit)) == 0)
                {
                    continue;
                }
                if(used >= remaining)
                {
                    break;
                }
                machine.SystemBus.WriteDoubleWord(
                    memory + used * sizeof(uint), timestamp);
                used++;
            }
            // STM32DMA's GPIO request implementation expands a request on
            // this shared timer stream into four identical transfers.  That
            // is appropriate for outbound DMAR bursts, but input capture is
            // one CCR word per edge.  Present the exact memory and NDTR state
            // real hardware leaves behind; ArduPilot's own receive timeout
            // then disables the stream and decodes this buffer normally.
            dma.WriteDoubleWord(StreamCount(stream), remaining - used);
            RepliesInjected++;
        }

        private int EscForCapture(STM32_Timer timer)
        {
            // SpeedyBee shares each available capture DMA with its paired
            // motor. ArduPilot selects direct vs indirect timer input through
            // CCxS: TIM4 CCR2 receives TI1 (M1) or TI2 (M2), while TIM3 CCR4
            // receives TI3 (M4) or TI4 (M3).
            var selection = ReferenceEquals(timer, timer4)
                ? (timer.ReadDoubleWord(CaptureMode1) >> 8) & 3
                : (timer.ReadDoubleWord(CaptureMode2) >> 8) & 3;
            if(ReferenceEquals(timer, timer4))
            {
                if(selection == 2) return 0;
                if(selection == 1) return 1;
            }
            else
            {
                if(selection == 2) return 3;
                if(selection == 1) return 2;
            }
            return -1;
        }

        private static uint EncodeGcr(ushort frame)
        {
            uint encoded = 0;
            for(var shift = 12; shift >= 0; shift -= 4)
            {
                encoded = (encoded << 5) | GcrTable[(frame >> shift) & 0xF];
            }
            return encoded;
        }

        private static bool HasNormalChecksum(ushort frame)
        {
            var payload = frame >> 4;
            return (frame & 0xF) ==
                ((payload ^ (payload >> 4) ^ (payload >> 8)) & 0xF);
        }

        private static bool IsBidirectional(ushort frame)
        {
            var payload = frame >> 4;
            return (frame & 0xF) ==
                ((~(payload ^ (payload >> 4) ^ (payload >> 8))) & 0xF);
        }

        private static byte DshotType(uint autoReload)
        {
            // The F405 timer clock is 84MHz.  ARR is close to 559/279/139
            // for DShot150/300/600 respectively.
            if(autoReload > 400) return Dshot150;
            if(autoReload > 200) return Dshot300;
            return Dshot600;
        }

        private uint TimerDmarAddress(STM32_Timer timer)
        {
            return ReferenceEquals(timer, timer3) ? Timer3Base + DmaAddress :
                Timer4Base + DmaAddress;
        }

        private static long StreamConfiguration(int stream) =>
            StreamBase + StreamStride * stream;
        private static long StreamCount(int stream) =>
            StreamConfiguration(stream) + 0x04;
        private static long StreamPeripheral(int stream) =>
            StreamConfiguration(stream) + 0x08;
        private static long StreamMemory(int stream) =>
            StreamConfiguration(stream) + 0x0C;

        private static ushort Get16(byte[] data, int offset) =>
            (ushort)(data[offset] | data[offset + 1] << 8);
        private static void Put16(byte[] data, int offset, ushort value)
        {
            data[offset] = (byte)value;
            data[offset + 1] = (byte)(value >> 8);
        }

        private readonly IMachine machine;
        private readonly STM32DMA dma;
        private readonly STM32_Timer timer3;
        private readonly STM32_Timer timer4;
        private readonly UdpClient[] sockets;
        private readonly ushort?[] cachedReplies;
        private readonly object sync = new object();
        private volatile bool disposed;

        private static readonly uint[] GcrTable = {
            0x19, 0x1B, 0x12, 0x13, 0x1D, 0x15, 0x16, 0x17,
            0x1A, 0x09, 0x0A, 0x0B, 0x1E, 0x0D, 0x0E, 0x0F,
        };

        private const ushort Magic = 0x4453;
        private const ushort IdleHigh = 1;
        private const byte Dshot150 = 1;
        private const byte Dshot300 = 2;
        private const byte Dshot600 = 3;
        private const int EscCount = 4;
        private const int ChannelsPerTimer = 4;
        private static readonly int[] Timer3Escs = { -1, -1, 3, 2 };
        private static readonly int[] Timer4Escs = { 0, 1, -1, -1 };
        private const int DshotPreamble = 1;
        private const int DshotBits = 16;
        private const int DshotWords = 19 * ChannelsPerTimer;
        private const int GcrBits = 20;
        private const uint CaptureTick = 16;
        private const int ReceiveTimeoutMs = 100;

        private const int OutputTimer3Stream = 2;
        private const int OutputTimer4Stream = 6;
        private const int CaptureTimer3Stream = 2;
        private const int CaptureTimer4Stream = 3;
        private const long StreamBase = 0x10;
        private const long StreamStride = 0x18;
        private const uint StreamEnable = 1;
        private const uint DirectionMask = 3u << 6;
        private const uint PeripheralToMemory = 0;
        private const uint MemoryToPeripheral = 1u << 6;

        private const uint Timer3Base = 0x40000400;
        private const uint Timer4Base = 0x40000800;
        private const uint DmaAddress = 0x4C;
        private const long AutoReload = 0x2C;
        private const long CaptureMode1 = 0x18;
        private const long CaptureMode2 = 0x1C;
    }
}
