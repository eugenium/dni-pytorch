from __future__ import print_function
import argparse
import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
from torchvision import datasets, transforms
from torch.autograd import Variable
import dni

# Training settings
parser = argparse.ArgumentParser(description='PyTorch MNIST Example')
parser.add_argument('--batch-size', type=int, default=256, metavar='N',
                    help='input batch size for training (default: 64)')
parser.add_argument('--test-batch-size', type=int, default=1000, metavar='N',
                    help='input batch size for testing (default: 1000)')
parser.add_argument('--epochs', type=int, default=10, metavar='N',
                    help='number of epochs to train (default: 10)')
parser.add_argument('--lr', type=float, default=0.1, metavar='LR',
                    help='learning rate (default: 0.001)')
parser.add_argument('--no-cuda', action='store_true', default=False,
                    help='disables CUDA training')
parser.add_argument('--seed', type=int, default=1, metavar='S',
                    help='random seed (default: 1)')
parser.add_argument('--log-interval', type=int, default=10, metavar='N',
                    help='how many batches to wait before logging training status')
parser.add_argument('--dni', action='store_true', default=False,
                    help='enable DNI')
parser.add_argument('--context', action='store_true', default=False,
                    help='enable context (label conditioning) in DNI')
args = parser.parse_args()
args.cuda = not args.no_cuda and torch.cuda.is_available()

torch.manual_seed(args.seed)
if args.cuda:
    torch.cuda.manual_seed(args.seed)

def lr_scheduler(max_epoch,lr_0,lr_final,epoch):
    epoch_stabilization = 0
    if(epoch<max_epoch-epoch_stabilization):
        lr = lr_final + (lr_0-lr_final)*(1-(1.0*epoch/(1.0*(max_epoch-epoch_stabilization-1))))
    else:
        lr = lr_final
    return lr

print('==> Preparing data..')
transform_train = transforms.Compose([
    transforms.RandomCrop(32, padding=4),
    transforms.RandomHorizontalFlip(),
    transforms.ToTensor(),
    transforms.Normalize((0.4914, 0.4822, 0.4465), (0.2023, 0.1994, 0.2010)),
])

transform_test = transforms.Compose([
    transforms.ToTensor(),
    transforms.Normalize((0.4914, 0.4822, 0.4465), (0.2023, 0.1994, 0.2010)),
])

trainset_class = CIFAR10(root='.', train=True, download=True,transform=transform_train)
trainloader_classifier = torch.utils.data.DataLoader(trainset_class, batch_size=args.batch_size, shuffle=True, num_workers=args.workers)
testset = torchvision.datasets.CIFAR10(root='.', train=False, download=True, transform=transform_test)
testloader = torch.utils.data.DataLoader(testset, batch_size=256, shuffle=False, num_workers=2)


def one_hot(indexes, n_classes):
    result = torch.FloatTensor(indexes.size() + (n_classes,))
    if args.cuda:
        result = result.cuda()
    result.zero_()
    indexes_rank = len(indexes.size())
    result.scatter_(
        dim=indexes_rank,
        index=indexes.data.unsqueeze(dim=indexes_rank),
        value=1
    )
    return Variable(result)

#CIFAR-10 CNN The hidden layers are all convolutional
#layers with 128 5 × 5 filters with resolution preserving
#padding, followed by batch-normalisation, ReLU and 3×3
#spatial max-pooling in the first layer and avg-pooling in
#the remaining ones. The synthetic gradient model has two
#hidden layers with 128 5×5 filters with resolution preserving padding, batch-normalisation and ReLU, followed by
#a final 128 5 × 5 filter convolutional layer with resolution
#preserving padding.
class Net(nn.Module):
    def __init__(self):
        super(Net, self).__init__()
        self.block = nn.ModuleList([])
        self.backward_interfaces = []

        self.block.append(nn.Sequential(
            nn.Conv2d(3, 128, kernel_size=5, padding=2),
            nn.BatchNorm2d(128), nn.ReLU,
            nn.MaxPool((3,3))
        ))
        self.block.append(nn.Sequential(
            nn.Conv2d(128, 128, kernel_size=5, padding=2),
            nn.BatchNorm2d(128), nn.ReLU,
            nn.AvgPool((3, 3))
        ))
        self.block.append(nn.Sequential(
            nn.Conv2d(128, 128, kernel_size=5, padding=2),
            nn.BatchNorm2d(128), nn.ReLU,
            nn.AvgPool((3, 3))
        ))
        if args.dni:
            self.backward_interfaces[0] = dni.BackwardInterface(ConvSynthesizer())
            self.backward_interfaces[1] = dni.BackwardInterface(ConvSynthesizer())
            self.backward_interfaces[2] = dni.BackwardInterface(ConvSynthesizer())
        self.fc1 = nn.Linear(128*9, 10)

    def forward(self, x, y=None):
        for i in range(3):
            x = self.block[i](x)
            if args.dni and self.training:
                if args.context:
                    context = one_hot(y, 10)
                else:
                    context = None
                with dni.synthesizer_context(context):
                    x = self.backward_interfaces[i](x)
        x = F.relu(x)
        x = x.view(-1, 128*9)
        x = self.fc1(x)
        return F.log_softmax(x)


class ConvSynthesizer(nn.Module):
    def __init__(self):
        super(ConvSynthesizer, self).__init__()
        self.input_trigger = nn.Conv2d(128, 128, kernel_size=5, padding=4)
        self.input_context = nn.Linear(128, 128)
        self.hidden = nn.Conv2d(128, 128, kernel_size=5, padding=4)
        self.output = nn.Conv2d(128, 128, kernel_size=5, padding=4)
        # zero-initialize the last layer, as in the paper
        nn.init.constant(self.output.weight, 0)

    def forward(self, trigger, context):
        x = self.input_trigger(trigger)
        if context is not None:
            x += (
                self.input_context(context).unsqueeze(2)
                                           .unsqueeze(3)
                                           .expand_as(x)
            )
        x = self.hidden(F.relu(x))
        return self.output(F.relu(x))


model = Net()
if args.cuda:
    model.cuda()

def train(epoch):
    model.train()
    lr = lr_scheduler(args.epochs, 0.1, 0.005, epoch)
    optimizer = optim.SGD(model.parameters(), lr=lr, momentum=0.9, weight_decay=5e-4)

    for batch_idx, (data, target) in enumerate(train_loader):
        if args.cuda:
            data, target = data.cuda(), target.cuda()
        data, target = Variable(data), Variable(target)
        optimizer.zero_grad()
        output = model(data, target)
        loss = F.nll_loss(output, target)
        loss.backward()
        optimizer.step()
        if batch_idx % args.log_interval == 0:
            print('Train Epoch: {} [{}/{} ({:.0f}%)]\tLoss: {:.6f}'.format(
                epoch, batch_idx * len(data), len(train_loader.dataset),
                100. * batch_idx / len(train_loader), loss.data[0]))

def test():
    model.eval()
    test_loss = 0
    correct = 0
    for data, target in test_loader:
        if args.cuda:
            data, target = data.cuda(), target.cuda()
        data, target = Variable(data, volatile=True), Variable(target)
        output = model(data)
        test_loss += F.nll_loss(output, target, size_average=False).data[0] # sum up batch loss
        pred = output.data.max(1, keepdim=True)[1] # get the index of the max log-probability
        correct += pred.eq(target.data.view_as(pred)).cpu().sum()

    test_loss /= len(test_loader.dataset)
    print('\nTest set: Average loss: {:.4f}, Accuracy: {}/{} ({:.0f}%)\n'.format(
        test_loss, correct, len(test_loader.dataset),
        100. * correct / len(test_loader.dataset)))


for epoch in range(1, args.epochs + 1):
    train(epoch)
    test()
