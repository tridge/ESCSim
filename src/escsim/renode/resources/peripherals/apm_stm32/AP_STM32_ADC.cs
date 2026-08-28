// STM32F4 ADC model with the 16-bit data-register access used by ChibiOS DMA
// and the injected conversion sequence used by Betaflight for VREFINT and the
// internal temperature sensor. Renode's stock STM32_ADC implements regular
// conversions but currently ignores CR2.JSWSTART and the JDR registers.
using System.Collections.Generic;

using Antmicro.Renode.Core;
using Antmicro.Renode.Peripherals;
using Antmicro.Renode.Peripherals.Bus;

namespace Antmicro.Renode.Peripherals.Analog
{
    public class AP_STM32_ADC : STM32_ADC, IWordPeripheral,
        IDoubleWordPeripheral, IPeripheral
    {
        public AP_STM32_ADC(IMachine machine) : base(machine)
        {
        }

        public new void Reset()
        {
            base.Reset();
            injectedStatus = 0;
            for(var index = 0; index < injectedData.Length; index++)
            {
                injectedData[index] = 0;
            }
        }

        public new uint ReadDoubleWord(long offset)
        {
            if(offset == Status)
            {
                return base.ReadDoubleWord(offset) | injectedStatus;
            }
            if(offset >= InjectedData1 && offset <= InjectedData4 &&
               (offset - InjectedData1) % 4 == 0)
            {
                return injectedData[(offset - InjectedData1) / 4];
            }
            return base.ReadDoubleWord(offset);
        }

        public new void WriteDoubleWord(long offset, uint value)
        {
            if(offset == Status)
            {
                // STM32F4 ADC status flags are cleared by writing zero.
                injectedStatus &= value;
                base.WriteDoubleWord(offset, value);
                return;
            }
            if(offset == Control2 && (value & InjectedSoftwareStart) != 0)
            {
                // JSWSTART is set by software and cleared by hardware when
                // the conversion begins. The conversion is instantaneous at
                // the abstraction level of Renode's regular ADC model.
                base.WriteDoubleWord(offset, value & ~InjectedSoftwareStart);
                ConvertInjected();
                return;
            }
            base.WriteDoubleWord(offset, value);
        }

        public ushort ReadWord(long offset)
        {
            return (ushort)ReadDoubleWord(offset);
        }

        public void WriteWord(long offset, ushort value)
        {
            WriteDoubleWord(offset, value);
        }

        public new void FeedSample(uint value, uint channel, int repeat = -1)
        {
            samples[channel] = value & 0xFFFF;
            base.FeedSample(value, channel, repeat);
        }

        private void ConvertInjected()
        {
            var sequence = base.ReadDoubleWord(InjectedSequence);
            var count = (int)((sequence >> 20) & 3) + 1;
            var firstSlot = 4 - count;
            for(var rank = 0; rank < count; rank++)
            {
                var channel = (sequence >> ((firstSlot + rank) * 5)) & 0x1F;
                injectedData[rank] = samples.TryGetValue(channel, out var sample)
                    ? sample
                    : 0;
            }
            injectedStatus |= InjectedEndOfConversion;
        }

        private readonly Dictionary<uint, uint> samples =
            new Dictionary<uint, uint>();
        private readonly uint[] injectedData = new uint[4];
        private uint injectedStatus;

        private const long Status = 0x00;
        private const long Control2 = 0x08;
        private const long InjectedSequence = 0x38;
        private const long InjectedData1 = 0x3C;
        private const long InjectedData4 = 0x48;
        private const uint InjectedEndOfConversion = 1U << 2;
        private const uint InjectedSoftwareStart = 1U << 22;
    }
}
