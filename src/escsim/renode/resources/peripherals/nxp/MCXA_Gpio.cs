//
// MCXA153 GPIO. The firmware uses exactly one read path: byte reads of
// PDR[n] at offset 0x60+n (the throttle pin state in getInputPinState()
// and the inverted-dshot idle detection). Pin states arrive as GPIO
// connections from the throttle generator; everything else the SDK
// writes (PDDR, ICR, ...) is stored and read back.
//
using Antmicro.Renode.Core;
using Antmicro.Renode.Logging;
using Antmicro.Renode.Peripherals;
using Antmicro.Renode.Peripherals.Bus;
using Antmicro.Renode.Peripherals.Timers;
using System.Collections.Generic;
using System.Linq;

namespace Antmicro.Renode.Peripherals.Miscellaneous
{
    public class MCXA_Gpio : IDoubleWordPeripheral, IBytePeripheral,
                             IKnownSize, IGPIOReceiver, INumberedGPIOOutput
    {
        public MCXA_Gpio(IMachine machine)
        {
            this.machine = machine;
            var conns = new Dictionary<int, IGPIO>();
            for(var i = 0; i < pins.Length; i++)
            {
                conns[i] = new GPIO();
            }
            Connections = conns;
            Reset();
        }

        // the guest's pin outputs (PDOR/PSOR/PCOR/PTOR), so the throttle
        // generator can watch the signal pin for one-wire serial TX
        public IReadOnlyDictionary<int, IGPIO> Connections { get; }

        public long Size => 0x1000;

        public void Reset()
        {
            regs.Clear();
            for(var i = 0; i < pins.Length; i++)
            {
                pins[i] = false;
                lowSamplePending[i] = false;
            }
            captureTimerInverted = false;
            SetOutputs(0);
        }

        public void OnGPIO(int number, bool value)
        {
            if(number < 0 || number >= pins.Length)
            {
                return;
            }
            var was = pins[number];
            pins[number] = value;
            if(was && !value)
            {
                // A153's pre-inversion capture DMA completes on this
                // falling edge.  The CPU must observe that level even if
                // Renode dispatches the following rising edge before the
                // DMA ISR; real hardware samples the pin before that edge.
                lowSamplePending[number] = true;
            }
            else if(!was && value && CaptureTimerIsInverted())
            {
                // Once firmware swaps the capture polarity, DMA completes
                // on the rising edge and the ISR correctly observes high.
                lowSamplePending[number] = false;
            }
        }

        public uint ReadDoubleWord(long offset)
        {
            if(offset >= Pdr && offset < Pdr + 32)
            {
                return SamplePin((int)(offset - Pdr)) ? 1u : 0u;
            }
            if(offset == Pdir)
            {
                // the bootloader's gpio_read polls the whole input
                // register where the firmware reads PDR bytes
                var v32 = 0u;
                for(var i = 0; i < pins.Length; i++)
                {
                    if(SamplePin(i))
                    {
                        v32 |= 1u << i;
                    }
                }
                return v32;
            }
            uint v;
            regs.TryGetValue(offset, out v);
            return v;
        }

        public void WriteDoubleWord(long offset, uint value)
        {
            switch(offset)
            {
            case Pdor:
                SetOutputs(value);
                break;
            case Psor:
                SetOutputs(pdor | value);
                return;
            case Pcor:
                SetOutputs(pdor & ~value);
                return;
            case Ptor:
                SetOutputs(pdor ^ value);
                return;
            }
            regs[offset] = value;
        }

        private void SetOutputs(uint value)
        {
            var changed = pdor ^ value;
            pdor = value;
            for(var i = 0; changed != 0; i++, changed >>= 1)
            {
                if((changed & 1) != 0)
                {
                    Connections[i].Set((value >> i & 1) != 0);
                }
            }
        }

        public byte ReadByte(long offset)
        {
            if(offset >= Pdr && offset < Pdr + 32)
            {
                return SamplePin((int)(offset - Pdr)) ? (byte)1 : (byte)0;
            }
            return 0;
        }

        public void WriteByte(long offset, byte value)
        {
            if(offset >= Pdr && offset < Pdr + 32)
            {
                pins[offset - Pdr] = value != 0;
            }
        }

        private const long Pdr = 0x60;
        private const long Pdor = 0x40;
        private const long Pdir = 0x50;
        private const long Psor = 0x44;
        private const long Pcor = 0x48;
        private const long Ptor = 0x4C;

        private bool SamplePin(int pin)
        {
            if(lowSamplePending[pin])
            {
                lowSamplePending[pin] = false;
                return false;
            }
            return pins[pin];
        }

        private bool CaptureTimerIsInverted()
        {
            if(!captureTimerInverted)
            {
                // Polarity detection is monotonic until a machine reset.
                // Cache that fact, not a heuristically selected timer: this
                // remains correct if a future A153 platform adds another
                // CTIMER that configures capture before CTIMER0.
                captureTimerInverted = machine.GetPeripheralsOfType<MCXA_Ctimer>()
                    .Any(timer => timer.InvertedDshotCapture);
            }
            return captureTimerInverted;
        }

        private uint pdor;

        private readonly IMachine machine;
        private readonly Dictionary<long, uint> regs = new Dictionary<long, uint>();
        private readonly bool[] pins = new bool[32];
        private readonly bool[] lowSamplePending = new bool[32];
        private bool captureTimerInverted;
    }
}
