/*

FIX NVMe

 */



DefinitionBlock ("", "SSDT", 1, "mano", "NVME", 0x00003000)
{
    External (_SB_.PCI0, DeviceObj)
    External (_SB_.PCI0.RP01, DeviceObj)
    External (_SB_.PCI0.RP01.D073, DeviceObj)
    External (DTGP, MethodObj)    // 5 Arguments

    If (_OSI ("Darwin"))
    {
        Device (_SB.PCI0.RP01.SSD0)
        {
            Name (_ADR, Zero)  // _ADR: Address
            Method (_DSM, 4, NotSerialized)  // _DSM: Device-Specific Method
            {
                If ((Arg2 == Zero))
                {
                    Return (Buffer (One)
                    {
                         0x03                                             // .
                    })
                }

                Local0 = Package (0x10)
                    {
                        "use-msi", 
                        Buffer (One)
                        {
                             0x01                                             // .
                        }, 

                        "built-in", 
                        Buffer (0x09)
                        {
                            "NVMe SSD"
                        }, 

                        "device-id", 
                        Buffer (0x04)
                        {
                             0x01, 0xA8, 0x00, 0x00                           // ....
                        }, 

                        "class-code", 
                        Buffer (0x04)
                        {
                             0x02, 0x08, 0x01, 0x00                           // ....
                        }, 

                        "name", 
                        Buffer (0x06)
                        {
                            "SM960"
                        }, 

                        "model", 
                        Buffer (0x1A)
                        {
                            "Samsung SSD 960 EVO 500GB"
                        }, 

                        "device_type", 
                        Buffer (0x17)
                        {
                            "NVM Express Controller"
                        }
                    }
                DTGP (Arg0, Arg1, Arg2, Arg3, RefOf (Local0))
                Return (Local0)
            }

            Method (_PRW, 0, NotSerialized)  // _PRW: Power Resources for Wake
            {
                Return (Package (0x02)
                {
                    0x6D, 
                    Zero
                })
            }
        }

        Name (_SB.PCI0.RP01.D073._STA, Zero)  // _STA: Status
    }
}

