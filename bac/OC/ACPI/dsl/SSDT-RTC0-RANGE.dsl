/*
 * On certain motherboards(mainly Asus X299 boards), not all ports are
 * mapped in the RTC device. For the majority of the time, users will not notice 
 * this issue though in extreme circumstances macOS may halt in early booting.
 * Most prominently seen around the PCI Configuration stage with macOS 11 Big Sur.
 * 
 * To resolve this, we'll want to create a new RTC device(PNP0B00) with the correct
 * range.
 * 
 * Note that due to AWAC systems having an _STA method already defined, attempting
 * to set another _STA method in your RTC device will conflict. To resolve this,
 * SSDT-AWAC should be removed and instead opt for this SSDT instead.
 */
DefinitionBlock ("", "SSDT", 2, "ACDT", "RtcRange", 0x00000000)
{
    External (_SB_.PCI0.LPC0, DeviceObj)
    External (_SB_.PCI0.LPC0.RTC, DeviceObj)

    Scope (_SB.PCI0.LPC0)
    {
        Scope (RTC)
        {
            Method (_STA, 0, NotSerialized)  // _STA: Status
            {
                If (_OSI ("Darwin"))
                {
                    Return (Zero)
                }
                Else
                {
                    Return (0x0F)
                }
            }
        }
        
        Device (RTC0)
        {
            Name (_HID, EisaId ("PNP0B00"))  // _HID: Hardware ID
            Name (_CRS, ResourceTemplate ()  // _CRS: Current Resource Settings
            {
                IO (Decode16,
                    0x0070,             // Range Minimum 1
                    0x0070,             // Range Maximum 1
                    0x01,               // Alignment 1
                    0x04,               // Length 1      (Expanded to include 0x72 and 0x73)
                    )
                IO (Decode16,
                    0x0074,             // Range Minimum 2
                    0x0074,             // Range Maximum 2
                    0x01,               // Alignment 2
                    0x04,               // Length 2
                    )
                IRQNoFlags ()
                    {8}
            })
            Method (_STA, 0, NotSerialized)  // _STA: Status
            {
                If (_OSI ("Darwin"))
                {
                    Return (0x0F)
                }
                Else
                {
                    Return (Zero)
                }
            }
        }
    }
}

