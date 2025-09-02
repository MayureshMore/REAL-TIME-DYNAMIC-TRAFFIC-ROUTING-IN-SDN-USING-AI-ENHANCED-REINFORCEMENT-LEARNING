# scripts/topos/two_path.py
# Two disjoint paths between h1 and h2 for baseline routing tests
# h1 - s1 - s3 - s5 - h2
# h1 - s2 - s4 - s5 - h2


from mininet.net import Mininet
from mininet.topo import Topo
from mininet.link import TCLink
from mininet.node import RemoteController, OVSSwitch
from mininet.cli import CLI
import argparse




class TwoPathTopo(Topo):
    def build(self, bw=10):
    h1 = self.addHost('h1')
h2 = self.addHost('h2')
s1 = self.addSwitch('s1')
s2 = self.addSwitch('s2')
s3 = self.addSwitch('s3')
s4 = self.addSwitch('s4')
s5 = self.addSwitch('s5')


# Path A: h1-s1-s3-s5-h2
self.addLink(h1, s1, cls=TCLink, bw=bw)
self.addLink(s1, s3, cls=TCLink, bw=bw)
self.addLink(s3, s5, cls=TCLink, bw=bw)
self.addLink(s5, h2, cls=TCLink, bw=bw)


# Path B: h1-s2-s4-s5-h2
self.addLink(h1, s2, cls=TCLink, bw=bw)
self.addLink(s2, s4, cls=TCLink, bw=bw)
self.addLink(s4, s5, cls=TCLink, bw=bw)




def main():
    parser = argparse.ArgumentParser()
parser.add_argument('--controller_ip', default='127.0.0.1')
parser.add_argument('--controller_port', type=int, default=6633)
parser.add_argument('--bw', type=int, default=10, help='Link bandwidth (Mb/s)')
args = parser.parse_args()


topo = TwoPathTopo(bw=args.bw)
net = Mininet(topo=topo, link=TCLink, controller=None, switch=OVSSwitch, autoSetMacs=True)
c0 = net.addController('c0', controller=RemoteController, ip=args.controller_ip, port=args.controller_port)


net.start()


print("Testing connectivity...")
net.pingAll()


print("Start an iperf3 server on h1 and client on h2 (example):")
print(" h1: iperf3 -s & | h2: iperf3 -c $(h1.IP()) -t 10")


CLI(net)
net.stop()




if __name__ == '__main__':
    main()