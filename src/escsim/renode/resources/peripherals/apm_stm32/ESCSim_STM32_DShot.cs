// Bridges the SpeedyBeeF405 timer DMA waveform to four independent ESCSim ESC
// signal sockets.  ArduPilot interleaves all four timer channels in one DMAR
// buffer, while Betaflight uses one 18-word buffer per timer channel.  Decode
// both forms so the bridge preserves the actual packet, including commands and
// the inverted checksum used by bidirectional DShot.
//
// Bidirectional replies arrive from the ESC as ESCSim's GCR-decoded 16-bit
// frame.  They are converted back to timer input-capture edge timestamps and
// delivered through the same STM32 DMA streams firmware configured for the
// physical pins.  The reply cached from the preceding output cycle is used:
// separate Renode processes do not share virtual time, and this one-cycle
// pipeline keeps the FC's 80us capture window deterministic.
using System;
using System.Collections.Generic;
using System.Linq;
using System.Net;
using System.Net.Sockets;
using System.Threading;

using Antmicro.Renode.Core;
using Antmicro.Renode.Core.Structure.Registers;
using Antmicro.Renode.Peripherals;
using Antmicro.Renode.Peripherals.Bus;
using Antmicro.Renode.Peripherals.DMA;
using Antmicro.Renode.Peripherals.GPIOPort;
using Antmicro.Renode.Peripherals.Timers;
using Antmicro.Renode.Time;

namespace Antmicro.Renode.Peripherals.Miscellaneous
{
    public sealed class ESCSim_STM32_DShot : IDoubleWordPeripheral, IKnownSize,
        IGPIOReceiver, INumberedGPIOOutput, IDisposable
    {
        public ESCSim_STM32_DShot(IMachine machine, STM32DMA dma,
            STM32_GPIOPort gpio,
            STM32_Timer timer2, STM32_Timer timer3, STM32_Timer timer4,
            int esc1Port, int esc2Port, int esc3Port, int esc4Port,
            int esc1StatePort = 0, int esc2StatePort = 0,
            int esc3StatePort = 0, int esc4StatePort = 0)
        {
            this.machine = machine;
            this.dma = dma;
            this.gpio = gpio;
            this.timer2 = timer2;
            this.timer3 = timer3;
            this.timer4 = timer4;
            Connections = Enumerable.Range(0, EscCount)
                .ToDictionary(index => index, _ => (IGPIO)new GPIO());
            sockets = new UdpClient[EscCount];
            stateSockets = new UdpClient[EscCount];
            cachedReplies = new ushort?[EscCount];
            serialReplyBytes = new Queue<byte>[EscCount];
            serialReplyReady = new AutoResetEvent[EscCount];
            serialRequestBytes = new List<byte>[EscCount];
            directRequestBytes = new List<byte>[EscCount];
            directReplyBytes = new Queue<byte>[EscCount];
            directReplyComplete = new bool[EscCount];
            fastReplyPackets = new Queue<byte[]>[EscCount];
            fastReplyReady = new AutoResetEvent[EscCount];
            lineLevels = new bool[EscCount];
            decoding = new bool[EscCount];
            replyDriving = new bool[EscCount];
            serialSessions = new bool[EscCount];
            expectingBufferData = new bool[EscCount];
            serialGeneration = new uint[EscCount];
            var ports = new[] { esc1Port, esc2Port, esc3Port, esc4Port };
            var statePorts = new[] {
                esc1StatePort, esc2StatePort, esc3StatePort, esc4StatePort,
            };
            for(var index = 0; index < EscCount; index++)
            {
                serialReplyBytes[index] = new Queue<byte>();
                serialReplyReady[index] = new AutoResetEvent(false);
                serialRequestBytes[index] = new List<byte>();
                directRequestBytes[index] = new List<byte>();
                directReplyBytes[index] = new Queue<byte>();
                fastReplyPackets[index] = new Queue<byte[]>();
                fastReplyReady[index] = new AutoResetEvent(false);
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
                if(statePorts[index] > 0)
                {
                    stateSockets[index] = new UdpClient();
                    stateSockets[index].Connect(IPAddress.Loopback,
                        statePorts[index]);
                }
            }

            foreach(var stream in ObservedStreams)
            {
                var captured = stream;
                dma.RegistersCollection.AddBeforeWriteHook(
                    StreamConfiguration(stream),
                    (offset, value) => ObserveStream(captured, value));
            }
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
            LastFrame = 0;
            Array.Clear(lastFrames, 0, lastFrames.Length);
            BidirectionalFrames = 0;
            LastDshotType = 0;
            SerialRequests = 0;
            SerialReplies = 0;
            lock(serialSync)
            {
                for(var index = 0; index < EscCount; index++)
                {
                    serialReplyBytes[index].Clear();
                    serialRequestBytes[index].Clear();
                    directRequestBytes[index].Clear();
                    directReplyBytes[index].Clear();
                    directReplyComplete[index] = false;
                    fastReplyPackets[index].Clear();
                    decoding[index] = false;
                    replyDriving[index] = false;
                    serialSessions[index] = false;
                    expectingBufferData[index] = false;
                    serialGeneration[index]++;
                }
            }
        }

        public uint ReadDoubleWord(long offset)
        {
            switch(offset)
            {
            case 0x00: return FramesSent;
            case 0x04: return RepliesReceived;
            case 0x08: return RepliesInjected;
            case 0x0C: return LastFrame;
            case 0x10: return BidirectionalFrames;
            case 0x14: return LastDshotType;
            case 0x18: return SerialRequests;
            case 0x1C: return SerialReplies;
            default:
                if(offset >= LastFrameBase &&
                   offset < LastFrameBase + EscCount * 4)
                {
                    return lastFrames[(offset - LastFrameBase) / 4];
                }
                if(offset >= DirectReplyBase &&
                   offset < DirectReplyBase + EscCount * 4)
                {
                    return ReadDirectReply((int)((offset - DirectReplyBase) / 4));
                }
                return 0;
            }
        }

        public void WriteDoubleWord(long offset, uint value)
        {
            switch(offset)
            {
            case DirectTransmit:
                QueueDirectTransmit((int)((value >> 8) & 0xFF), (byte)value);
                break;
            case DirectFlush:
                FlushDirectTransmit((int)value);
                break;
            case DirectBufferAddress:
                directBufferAddress = value;
                break;
            case DirectBufferTransmit:
                QueueDirectBuffer(
                    (int)((value >> 8) & 0xFF),
                    (int)(value & 0xFF),
                    (value & DirectBufferAppendCrc) != 0);
                break;
            }
        }

        public long Size => 0x100;

        public uint FramesSent { get; private set; }
        public uint RepliesReceived { get; private set; }
        public uint RepliesInjected { get; private set; }
        public uint LastFrame { get; private set; }
        public uint BidirectionalFrames { get; private set; }
        public uint LastDshotType { get; private set; }
        public uint SerialRequests { get; private set; }
        public uint SerialReplies { get; private set; }

        public IReadOnlyDictionary<int, IGPIO> Connections { get; private set; }

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
            foreach(var socket in stateSockets)
            {
                if(socket != null)
                {
                    socket.Close();
                }
            }
            foreach(var ready in serialReplyReady)
            {
                ready.Dispose();
            }
            foreach(var ready in fastReplyReady)
            {
                ready.Dispose();
            }
        }

        // Betaflight's 4-way implementation changes each motor pin from its
        // timer alternate function to ordinary GPIO and bit-bangs 19200 8N1.
        // The four ESCs are separate Renode machines, so this bridge decodes
        // those real pin writes into the existing serial-over-UDP wire format
        // and replays the bootloader's bytes onto the FC GPIO input.
        public void OnGPIO(int number, bool value)
        {
            if(number < 0 || number >= EscCount)
            {
                return;
            }
            lineLevels[number] = value;
            if(replyDriving[number] || !IsGpioOutput(number) ||
               decoding[number] || value)
            {
                return;
            }
            decoding[number] = true;
            serialBit[number] = 0;
            serialByte[number] = 0;
            machine.ScheduleAction(TimeInterval.FromMicroseconds(SerialHalfStartUs),
                _ => SampleSerialBit(number), name: "Betaflight 4-way RX sample");
        }

        private uint? ObserveStream(int stream, uint configuration)
        {
            if((configuration & StreamEnable) == 0)
            {
                return null;
            }

            var peripheral = dma.ReadDoubleWord(StreamPeripheral(stream));
            if(TryDirectRoute(peripheral, out var timer, out var esc))
            {
                if((configuration & DirectionMask) == MemoryToPeripheral)
                {
                    ObserveDirectDma(stream, timer, esc);
                }
                else if((configuration & DirectionMask) == PeripheralToMemory)
                {
                    return Schedule(() => Inject(stream, esc));
                }
                return null;
            }

            if(peripheral == TimerDmarAddress(timer3))
            {
                ObserveDma(stream, timer3, Timer3Escs, configuration);
                ObserveCapture(timer3, stream, configuration);
            }
            else if(peripheral == TimerDmarAddress(timer4))
            {
                ObserveDma(stream, timer4, Timer4Escs, configuration);
                ObserveCapture(timer4, stream, configuration);
            }
            return null;
        }

        private bool TryDirectRoute(uint peripheral, out STM32_Timer timer,
            out int esc)
        {
            switch(peripheral)
            {
            // SPEEDYBEEF405V5: PB1/TIM3_CH4 is motor 1, PB0/TIM3_CH3
            // motor 2, PB10/TIM2_CH3 motor 3, and PB11/TIM2_CH4 motor 4.
            case Timer3Base + Ccr4:
                timer = timer3;
                esc = 0;
                return true;
            case Timer3Base + Ccr3:
                timer = timer3;
                esc = 1;
                return true;
            case Timer2Base + Ccr3:
                timer = timer2;
                esc = 2;
                return true;
            case Timer2Base + Ccr4:
                timer = timer2;
                esc = 3;
                return true;
            default:
                timer = null;
                esc = -1;
                return false;
            }
        }

        private void ObserveDirectDma(int stream, STM32_Timer timer, int esc)
        {
            var count = (int)dma.ReadDoubleWord(StreamCount(stream));
            if(count < DirectDshotWords)
            {
                return;
            }
            var memory = dma.ReadDoubleWord(StreamMemory(stream));
            if(!TryDecodeDirect(memory, out var frame))
            {
                return;
            }
            SendFrame(esc, frame, DshotTypeDirect(timer),
                IsBidirectional(frame));
            FramesSent++;
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

        private bool TryDecodeDirect(uint memory, out ushort frame)
        {
            frame = 0;
            var any = false;
            var minimum = uint.MaxValue;
            var maximum = 0u;
            for(var bit = 0; bit < DshotBits; bit++)
            {
                var word = machine.SystemBus.ReadDoubleWord(
                    memory + (uint)(bit * sizeof(uint)));
                if(word != 0)
                {
                    minimum = Math.Min(minimum, word);
                    maximum = Math.Max(maximum, word);
                    any = true;
                }
            }
            if(!any)
            {
                return false;
            }
            // Betaflight programs ARR only after enabling the output DMA. In
            // bidirectional mode ARR is still 0xffffffff from input capture
            // when this hook runs, so distinguish zero/one by the two duty
            // widths in the already-complete buffer. This is also independent
            // of how the timer model scales its compare values.
            var threshold = (minimum + maximum) / 2;
            for(var bit = 0; bit < DshotBits; bit++)
            {
                var word = machine.SystemBus.ReadDoubleWord(
                    memory + (uint)(bit * sizeof(uint)));
                frame = (ushort)((frame << 1) | (word > threshold ? 1 : 0));
            }
            return HasNormalChecksum(frame) || IsBidirectional(frame);
        }

        private void SendFrame(int esc, ushort frame, uint autoReload,
            bool bidirectional)
        {
            SendFrame(esc, frame, DshotType(autoReload), bidirectional);
        }

        private void SendFrame(int esc, ushort frame, byte dshotType,
            bool bidirectional)
        {
            LastFrame = frame;
            lastFrames[esc] = frame;
            LastDshotType = dshotType;
            if(bidirectional)
            {
                BidirectionalFrames++;
            }
            var packet = new byte[8];
            Put16(packet, 0, Magic);
            packet[2] = dshotType;
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
                    if(packet.Length < 6 || Get16(packet, 0) != Magic)
                    {
                        continue;
                    }
                    if(packet[2] == FastSerialType)
                    {
                        var length = Get16(packet, 4);
                        if(packet.Length != FastSerialHeaderSize + length)
                        {
                            continue;
                        }
                        var reply = new byte[length];
                        Array.Copy(packet, FastSerialHeaderSize, reply, 0, length);
                        lock(serialSync)
                        {
                            fastReplyPackets[esc].Enqueue(reply);
                        }
                        fastReplyReady[esc].Set();
                        continue;
                    }
                    if(packet[2] == SerialType)
                    {
                        var length = packet[3];
                        var flags = Get16(packet, 4);
                        if(packet.Length != SerialHeaderSize + length ||
                           (flags & SerialTxDone) != 0 || length == 0)
                        {
                            continue;
                        }
                        lock(serialSync)
                        {
                            for(var offset = 0; offset < length; offset++)
                            {
                                serialReplyBytes[esc].Enqueue(
                                    packet[SerialHeaderSize + offset]);
                            }
                        }
                        serialReplyReady[esc].Set();
                        continue;
                    }
                    if(packet.Length < 8 || packet[3] != 4 ||
                       packet[2] > Dshot600)
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

        private bool IsGpioOutput(int esc)
        {
            var pin = GpioPins[esc];
            return ((gpio.ReadDoubleWord(GpioMode) >> (pin * 2)) & 3) ==
                GpioOutput;
        }

        private void SampleSerialBit(int esc)
        {
            if(!decoding[esc] || replyDriving[esc])
            {
                return;
            }
            if(lineLevels[esc])
            {
                serialByte[esc] |= (byte)(1 << serialBit[esc]);
            }
            serialBit[esc]++;
            if(serialBit[esc] < 8)
            {
                machine.ScheduleAction(TimeInterval.FromMicroseconds(SerialBitUs),
                    _ => SampleSerialBit(esc),
                    name: "Betaflight 4-way RX sample");
                return;
            }

            decoding[esc] = false;
            uint generation;
            lock(serialSync)
            {
                serialRequestBytes[esc].Add(serialByte[esc]);
                generation = ++serialGeneration[esc];
            }
            // At the final data-bit centre the stop bit has not begun yet.
            // Forty microseconds later Betaflight has either started the next
            // byte or released the pin to input while it waits for the ESC.
            machine.ScheduleAction(TimeInterval.FromMicroseconds(SerialReleaseCheckUs),
                _ => CheckSerialRelease(esc, generation),
                name: "Betaflight 4-way direction check");
            machine.ScheduleAction(TimeInterval.FromMicroseconds(SerialStaleUs),
                _ => DiscardStaleSerial(esc, generation),
                name: "Betaflight 4-way stale byte cleanup");
        }

        private void CheckSerialRelease(int esc, uint generation)
        {
            byte[] request;
            lock(serialSync)
            {
                if(generation != serialGeneration[esc] || IsGpioOutput(esc) ||
                   serialRequestBytes[esc].Count == 0)
                {
                    return;
                }
                request = serialRequestBytes[esc].ToArray();
                serialRequestBytes[esc].Clear();
            }

            var isBootProbe = IsBootProbe(request);
            var wanted = ExpectedSerialReply(esc, request, isBootProbe);
            // The GPIO sampler has already reconstructed a complete host
            // transaction. Use the same virtual-time-independent backend as
            // the optional firmware hooks, then put its reply back onto the
            // emulated pin. This keeps an unmodified FC build inside the web
            // configurator's wall-clock timeout even when its ESC runs slowly.
            var fastReply = FastSerialTransaction(esc, request);
            if(fastReply != null)
            {
                SerialRequests++;
                if(isBootProbe)
                {
                    serialSessions[esc] = IsBootReply(fastReply);
                }
                if(fastReply.Length > 0)
                {
                    SerialReplies += (uint)fastReply.Length;
                    ReplaySerialReply(esc, fastReply);
                }
                return;
            }
            if(isBootProbe && !serialSessions[esc])
            {
                // AM32 enters its loader when the signal is held high across
                // reset. This is the electrical action a real FC/ESC power
                // transition supplies; the state socket is the simulated
                // power-cycle line.
                serialSessions[esc] = HoldEscHighAndReset(esc);
            }

            SendSerial(esc, request);
            SerialRequests++;
            if(wanted == 0)
            {
                return;
            }
            var reply = WaitForSerialReply(esc, wanted);
            if(reply.Length == 0)
            {
                return;
            }
            SerialReplies += (uint)reply.Length;
            ReplaySerialReply(esc, reply);
        }

        private void DiscardStaleSerial(int esc, uint generation)
        {
            lock(serialSync)
            {
                if(generation == serialGeneration[esc] && IsGpioOutput(esc))
                {
                    serialRequestBytes[esc].Clear();
                }
            }
        }

        private void SendSerial(int esc, byte[] payload)
        {
            var socket = sockets[esc];
            if(socket == null || payload.Length == 0)
            {
                return;
            }
            for(var offset = 0; offset < payload.Length; offset += SerialMax)
            {
                var length = Math.Min(SerialMax, payload.Length - offset);
                var packet = new byte[SerialHeaderSize + length];
                Put16(packet, 0, Magic);
                packet[2] = SerialType;
                packet[3] = (byte)length;
                Put16(packet, 4, (ushort)(SerialIdleHigh |
                    (offset == 0 ? SerialGap : 0)));
                Array.Copy(payload, offset, packet, SerialHeaderSize, length);
                try
                {
                    socket.Send(packet, packet.Length);
                }
                catch(SocketException)
                {
                    // The ESC may be between its application and bootloader.
                    return;
                }
            }
        }

        private bool HoldEscHighAndReset(int esc)
        {
            var socket = sockets[esc];
            if(socket == null)
            {
                return false;
            }
            var line = new byte[8];
            Put16(line, 0, Magic);
            line[2] = LineType;
            line[3] = 4;
            Put16(line, 4, SerialIdleHigh);
            Put16(line, 6, 0);
            try
            {
                socket.Send(line, line.Length);
                // Let the ESC wire peripheral latch the high level before
                // resetting the MCU, matching the launcher's proven direct
                // 4-way sequence.
                Thread.Sleep(300);
                var state = stateSockets[esc];
                if(state != null)
                {
                    var reset = new byte[] {
                        (byte)(StateMagic & 0xFF), (byte)(StateMagic >> 8),
                        StateReset, 0,
                    };
                    state.Send(reset, reset.Length);
                    // With five Renode processes sharing the host, 100 ms of
                    // wall time can be less than the loader's startup path in
                    // its own virtual clock.  Wait long enough for its serial
                    // receive loop before sending the first probe.
                    Thread.Sleep(400);
                    return true;
                }
            }
            catch(SocketException)
            {
                // A retry from Betaflight will repeat the boot probe.
            }
            return false;
        }

        private byte[] WaitForSerialReply(int esc, int wanted,
            int timeoutMs = SerialHostTimeoutMs)
        {
            var result = new List<byte>(wanted);
            var deadline = Environment.TickCount64 + timeoutMs;
            while(result.Count < wanted)
            {
                lock(serialSync)
                {
                    while(serialReplyBytes[esc].Count > 0 && result.Count < wanted)
                    {
                        result.Add(serialReplyBytes[esc].Dequeue());
                    }
                }
                if(result.Count >= wanted)
                {
                    break;
                }
                var remaining = deadline - Environment.TickCount64;
                if(remaining <= 0 ||
                   !serialReplyReady[esc].WaitOne((int)Math.Min(remaining, 250)))
                {
                    if(Environment.TickCount64 >= deadline)
                    {
                        break;
                    }
                }
            }
            return result.ToArray();
        }

        private void ReplaySerialReply(int esc, byte[] data)
        {
            replyDriving[esc] = true;
            ulong delay = SerialReplyLeadUs;
            foreach(var value in data)
            {
                ScheduleLine(esc, false, delay); // start bit
                delay += SerialBitUs;
                for(var bit = 0; bit < 8; bit++)
                {
                    ScheduleLine(esc, ((value >> bit) & 1) != 0, delay);
                    delay += SerialBitUs;
                }
                ScheduleLine(esc, true, delay); // stop bit
                delay += SerialBitUs;
            }
            machine.ScheduleAction(TimeInterval.FromMicroseconds(delay), _ =>
            {
                Connections[esc].Set(true);
                replyDriving[esc] = false;
            }, name: "Betaflight 4-way reply complete");
        }

        // The ELF-addressed Betaflight hooks use these registers to preserve
        // the real AM32 UDP bootloader exchange while bypassing the FC's
        // 19200-baud GPIO bit loops.  The ordinary pin-level implementation
        // remains available for firmware without matching symbols.
        private void QueueDirectTransmit(int esc, byte value)
        {
            if(esc < 0 || esc >= EscCount)
            {
                return;
            }
            lock(serialSync)
            {
                if(directRequestBytes[esc].Count == 0)
                {
                    // Match a serial adapter's input flush at the start of a
                    // transaction.  In particular, don't let a late reply to
                    // a timed-out probe satisfy the following retry.
                    serialReplyBytes[esc].Clear();
                    directReplyBytes[esc].Clear();
                    directReplyComplete[esc] = false;
                }
                directRequestBytes[esc].Add(value);
            }
        }

        private void QueueDirectBuffer(int esc, int encodedLength, bool appendCrc)
        {
            if(esc < 0 || esc >= EscCount)
            {
                return;
            }
            var length = encodedLength == 0 ? 256 : encodedLength;
            var data = machine.SystemBus.ReadBytes(directBufferAddress, length);
            lock(serialSync)
            {
                if(directRequestBytes[esc].Count == 0)
                {
                    serialReplyBytes[esc].Clear();
                    directReplyBytes[esc].Clear();
                    directReplyComplete[esc] = false;
                }
                directRequestBytes[esc].AddRange(data);
                if(appendCrc)
                {
                    var crc = DirectBootCrc(data);
                    directRequestBytes[esc].Add((byte)crc);
                    directRequestBytes[esc].Add((byte)(crc >> 8));
                }
            }
        }

        private static ushort DirectBootCrc(byte[] data)
        {
            ushort crc = 0;
            foreach(var original in data)
            {
                var value = original;
                for(var bit = 0; bit < 8; bit++)
                {
                    crc = (ushort)(((value ^ crc) & 1) != 0
                        ? (crc >> 1) ^ 0xA001 : crc >> 1);
                    value >>= 1;
                }
            }
            return crc;
        }

        private void FlushDirectTransmit(int esc)
        {
            if(esc < 0 || esc >= EscCount)
            {
                return;
            }
            byte[] request;
            lock(serialSync)
            {
                if(directRequestBytes[esc].Count == 0)
                {
                    return;
                }
                request = directRequestBytes[esc].ToArray();
                directRequestBytes[esc].Clear();
            }

            var isBootProbe = IsBootProbe(request);
            var wanted = ExpectedSerialReply(esc, request, isBootProbe);
            var reply = FastSerialTransaction(esc, request);
            if(reply != null)
            {
                SerialRequests++;
                if(isBootProbe)
                {
                    serialSessions[esc] = IsBootReply(reply);
                }
            }
            else if(isBootProbe)
            {
                if(!serialSessions[esc])
                {
                    HoldEscHighAndReset(esc);
                }
                // The ESC can still be traversing its reset path when the
                // first UDP request arrives.  A real FC repeatedly sends the
                // boot pattern; resend it within one bounded window rather
                // than spending the whole timeout waiting for a request that
                // the loader never saw.  This also keeps Betaflight's entire
                // DeviceInitFlash command within the configurator's timeout.
                reply = ProbeDirectBootloader(esc, request);
                serialSessions[esc] = IsBootReply(reply);
            }
            else
            {
                SendSerial(esc, request);
                SerialRequests++;
                reply = wanted > 0 ? WaitForSerialReply(esc, wanted) :
                    new byte[0];
            }
            if(reply.Length > 0)
            {
                SerialReplies += (uint)reply.Length;
            }
            lock(serialSync)
            {
                foreach(var value in reply)
                {
                    directReplyBytes[esc].Enqueue(value);
                }
                directReplyComplete[esc] = true;
            }
        }

        private byte[] FastSerialTransaction(int esc, byte[] request)
        {
            var socket = sockets[esc];
            if(socket == null)
            {
                return null;
            }
            lock(serialSync)
            {
                fastReplyPackets[esc].Clear();
            }
            var packet = new byte[FastSerialHeaderSize + request.Length];
            Put16(packet, 0, Magic);
            packet[2] = FastSerialType;
            Put16(packet, 4, (ushort)request.Length);
            Array.Copy(request, 0, packet, FastSerialHeaderSize, request.Length);
            try
            {
                socket.Send(packet, packet.Length);
            }
            catch(SocketException)
            {
                return null;
            }
            var deadline = Environment.TickCount64 + FastSerialTimeoutMs;
            while(Environment.TickCount64 < deadline)
            {
                lock(serialSync)
                {
                    if(fastReplyPackets[esc].Count > 0)
                    {
                        return fastReplyPackets[esc].Dequeue();
                    }
                }
                var remaining = deadline - Environment.TickCount64;
                if(remaining <= 0)
                {
                    break;
                }
                fastReplyReady[esc].WaitOne((int)remaining);
            }
            return null;
        }

        private uint ReadDirectReply(int esc)
        {
            lock(serialSync)
            {
                if(directReplyBytes[esc].Count > 0)
                {
                    return DirectReplyValid | directReplyBytes[esc].Dequeue();
                }
                return directReplyComplete[esc] ? DirectReplyDone : 0;
            }
        }

        private byte[] ProbeDirectBootloader(int esc, byte[] request)
        {
            var deadline = Environment.TickCount64 + DirectBootProbeTimeoutMs;
            do
            {
                lock(serialSync)
                {
                    serialReplyBytes[esc].Clear();
                }
                SendSerial(esc, request);
                SerialRequests++;
                var remaining = deadline - Environment.TickCount64;
                if(remaining <= 0)
                {
                    break;
                }
                var reply = WaitForSerialReply(esc, 9,
                    (int)Math.Min(remaining, DirectBootProbeRetryMs));
                if(IsBootReply(reply))
                {
                    return reply;
                }
            }
            while(Environment.TickCount64 < deadline);
            return new byte[0];
        }

        private void ScheduleLine(int esc, bool level, ulong delay)
        {
            machine.ScheduleAction(TimeInterval.FromMicroseconds(delay),
                _ => Connections[esc].Set(level),
                name: "Betaflight 4-way reply bit");
        }

        private static bool IsBootProbe(byte[] request)
        {
            // Betaflight pads the legacy BLHeli token with zero bytes, and the
            // preamble length differs between builds. The token and its CRC
            // are the stable final nine bytes.
            var offset = request.Length - BootProbeSize;
            return offset >= 0 && request[offset] == 13
                && request[offset + 1] == (byte)'B'
                && request[offset + 2] == (byte)'L'
                && request[offset + 3] == (byte)'H'
                && request[offset + 4] == (byte)'e'
                && request[offset + 5] == (byte)'l'
                && request[offset + 6] == (byte)'i'
                && request[offset + 7] == 0xF4
                && request[offset + 8] == 0x7D;
        }

        private static bool IsBootReply(byte[] reply)
        {
            return reply.Length == 9 && reply[0] == (byte)'4' &&
                reply[1] == (byte)'7' && reply[2] == (byte)'1' &&
                reply[8] == BootSuccess;
        }

        private int ExpectedSerialReply(int esc, byte[] request, bool bootProbe)
        {
            if(bootProbe)
            {
                return 9;
            }
            if(request.Length == 0)
            {
                return 0;
            }
            // SET_BUFFER is a two-part transaction. Its six-byte header has
            // deliberately no acknowledgement; the loader acknowledges the
            // following data+CRC block, whose first byte is unconstrained.
            if(expectingBufferData[esc])
            {
                expectingBufferData[esc] = false;
                return 1;
            }
            if(request[0] == BootSetBuffer && request.Length == 6)
            {
                expectingBufferData[esc] = true;
                return 0;
            }
            switch(request[0])
            {
            case BootRun:
                if(request.Length == 4)
                {
                    // DeviceReset has returned this ESC to its application.
                    // A later configurator session must power-cycle it back
                    // into the loader before probing again.
                    serialSessions[esc] = false;
                    expectingBufferData[esc] = false;
                    return 0;
                }
                return 1;
            case BootRead:
                if(request.Length >= 2)
                {
                    return (request[1] == 0 ? 256 : request[1]) + 3;
                }
                return 1;
            default:
                return 1;
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
            Inject(stream, esc);
        }

        private void Inject(int stream, int esc)
        {
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

        private byte DshotTypeDirect(STM32_Timer timer)
        {
            // TIM2/3/4 run from the 84MHz APB1 timer clock. Betaflight sets
            // PSC to 27/13/6 for a 20-tick DShot150/300/600 bit period.
            var prescaler = timer.ReadDoubleWord(Prescaler);
            if(prescaler > 20) return Dshot150;
            if(prescaler > 9) return Dshot300;
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
        private readonly STM32_GPIOPort gpio;
        private readonly STM32_Timer timer2;
        private readonly STM32_Timer timer3;
        private readonly STM32_Timer timer4;
        private readonly UdpClient[] sockets;
        private readonly UdpClient[] stateSockets;
        private readonly ushort?[] cachedReplies;
        private readonly Queue<byte>[] serialReplyBytes;
        private readonly AutoResetEvent[] serialReplyReady;
        private readonly List<byte>[] serialRequestBytes;
        private readonly List<byte>[] directRequestBytes;
        private readonly Queue<byte>[] directReplyBytes;
        private readonly bool[] directReplyComplete;
        private readonly Queue<byte[]>[] fastReplyPackets;
        private readonly AutoResetEvent[] fastReplyReady;
        private readonly bool[] lineLevels;
        private readonly bool[] decoding;
        private readonly bool[] replyDriving;
        private readonly bool[] serialSessions;
        private readonly bool[] expectingBufferData;
        private readonly uint[] serialGeneration;
        private readonly uint[] lastFrames = new uint[EscCount];
        private readonly byte[] serialBit = new byte[EscCount];
        private readonly byte[] serialByte = new byte[EscCount];
        private readonly object sync = new object();
        private readonly object serialSync = new object();
        private volatile bool disposed;
        private uint directBufferAddress;

        private static readonly uint[] GcrTable = {
            0x19, 0x1B, 0x12, 0x13, 0x1D, 0x15, 0x16, 0x17,
            0x1A, 0x09, 0x0A, 0x0B, 0x1E, 0x0D, 0x0E, 0x0F,
        };

        private const ushort Magic = 0x4453;
        private const ushort IdleHigh = 1;
        private const byte Dshot150 = 1;
        private const byte Dshot300 = 2;
        private const byte Dshot600 = 3;
        private const byte SerialType = 4;
        private const byte LineType = 5;
        private const byte FastSerialType = 6;
        private const ushort SerialIdleHigh = 0x0001;
        private const ushort SerialGap = 0x0004;
        private const ushort SerialTxDone = 0x0008;
        private const int SerialHeaderSize = 6;
        private const int FastSerialHeaderSize = 6;
        private const int FastSerialTimeoutMs = 250;
        private const int BootProbeSize = 9;
        private const ushort StateMagic = 0x5353;
        private const byte StateReset = 9;
        private const byte BootRun = 0x00;
        private const byte BootRead = 0x03;
        private const byte BootSetBuffer = 0xFE;
        private const byte BootSuccess = 0x30;
        private const long GpioMode = 0x00;
        private const uint GpioOutput = 1;
        private const ulong SerialBitUs = 52;
        private const ulong SerialHalfStartUs = 78;
        private const ulong SerialReleaseCheckUs = 40;
        private const ulong SerialReplyLeadUs = 5;
        private const ulong SerialStaleUs = 2000;
        private const int SerialHostTimeoutMs = 12000;
        // Betaflight performs its own bounded BL_Connect retries.  Keep each
        // attempt short enough that a missed reset packet cannot consume the
        // configurator's five-second DeviceInitFlash timeout.
        private const int DirectBootProbeTimeoutMs = 1500;
        private const int DirectBootProbeRetryMs = 250;
        private const int SerialMax = 200;
        private const long DirectTransmit = 0x24;
        private const long DirectFlush = 0x28;
        private const long DirectBufferAddress = 0x2C;
        private const long DirectBufferTransmit = 0x30;
        private const uint DirectBufferAppendCrc = 1u << 16;
        private const long DirectReplyBase = 0x40;
        private const long LastFrameBase = 0x60;
        private const uint DirectReplyValid = 1u << 8;
        private const uint DirectReplyDone = 1u << 9;
        private const int EscCount = 4;
        private const int ChannelsPerTimer = 4;
        private static readonly int[] GpioPins = { 1, 0, 10, 11 };
        private static readonly int[] Timer3Escs = { -1, -1, 3, 2 };
        private static readonly int[] Timer4Escs = { 0, 1, -1, -1 };
        private const int DshotPreamble = 1;
        private const int DshotBits = 16;
        private const int DshotWords = 19 * ChannelsPerTimer;
        private const int DirectDshotWords = 18;
        private const int GcrBits = 20;
        private const uint CaptureTick = 16;
        private const int ReceiveTimeoutMs = 100;

        // The four Betaflight motor channels use streams 2, 7, 1 and 6.
        // Streams 2/3/6 also retain ArduPilot capture/output observation.
        // The UART3 RX pump deliberately hooks stream 1's count register so
        // its hardware resource conflict can coexist with this SxCR hook.
        private static readonly int[] ObservedStreams = { 1, 2, 3, 6, 7 };
        private const long StreamBase = 0x10;
        private const long StreamStride = 0x18;
        private const uint StreamEnable = 1;
        private const uint DirectionMask = 3u << 6;
        private const uint PeripheralToMemory = 0;
        private const uint MemoryToPeripheral = 1u << 6;

        private const uint Timer2Base = 0x40000000;
        private const uint Timer3Base = 0x40000400;
        private const uint Timer4Base = 0x40000800;
        private const uint Ccr3 = 0x3C;
        private const uint Ccr4 = 0x40;
        private const uint DmaAddress = 0x4C;
        private const long Prescaler = 0x28;
        private const long AutoReload = 0x2C;
        private const long CaptureMode1 = 0x18;
        private const long CaptureMode2 = 0x1C;
    }
}
