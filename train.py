import timeit
from datetime import datetime
import socket
import os
import glob
from tqdm import tqdm
import csv  # 新增导入CSV模块
from sklearn.metrics import roc_auc_score, accuracy_score, precision_score, recall_score

import torch
from tensorboardX import SummaryWriter
from torch import nn, optim
from torch.utils.data import DataLoader
from torch.autograd import Variable

from dataloaders.dataset import VideoDataset
from network.C3D_model import C3D

# Use GPU if available else revert to CPU
device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")
print("Device being used:", device)

nEpochs = 200  # Number of epochs for training
resume_epoch = 0  # Default is 0, change if want to resume
useTest = True  # See evolution of the test set when training
nTestInterval = 1  # Run on test set every nTestInterval epochs
snapshot = 10  # Store a model every snapshot epochs
lr = 1e-4  # Learning rate

dataset = 'hmdb51'  # Options: hmdb51 or ucf101

if dataset == 'hmdb51':
    num_classes = 2
elif dataset == 'ucf101':
    num_classes = 101
else:
    print('We only implemented hmdb and ucf datasets.')
    raise NotImplementedError

save_dir_root = os.path.join(os.path.dirname(os.path.abspath(__file__)))
exp_name = os.path.dirname(os.path.abspath(__file__)).split('/')[-1]

if resume_epoch != 0:
    runs = sorted(glob.glob(os.path.join(save_dir_root, 'run', 'run_*')))
    run_id = int(runs[-1].split('_')[-1]) if runs else 0
else:
    runs = sorted(glob.glob(os.path.join(save_dir_root, 'run', 'run_*')))
    run_id = int(runs[-1].split('_')[-1]) + 1 if runs else 0

save_dir = os.path.join(save_dir_root, 'run', 'run_' + str(run_id))
modelName = 'C3D'  # Options: C3D or R2Plus1D or R3D
saveName = modelName + '-' + dataset


# 新增定义保存数据到CSV的函数
def save_to_csv(epoch, phase, labels, probs, save_dir):
    """
    Saves the labels and probabilities to a CSV file for ROC curve generation.
    """
    os.makedirs(save_dir, exist_ok=True)  # 确保目录存在
    filename = os.path.join(save_dir, f'{phase}_epoch_{epoch}.csv')
    with open(filename, 'w', newline='') as file:
        writer = csv.writer(file)
        writer.writerow(['TrueLabel', 'Probability'])
        for label, prob in zip(labels, probs):
            writer.writerow([label, prob])
    print(f'Saved {phase} predictions to {filename}')


def train_model(dataset=dataset, save_dir=save_dir, num_classes=num_classes, lr=lr,
                num_epochs=nEpochs, save_epoch=snapshot, useTest=useTest, test_interval=nTestInterval):
    model = C3D(num_classes=num_classes, pretrained=False)
    criterion = nn.CrossEntropyLoss()
    optimizer = optim.Adam(model.parameters(), lr=lr, weight_decay=0.001)
    scheduler = optim.lr_scheduler.CosineAnnealingLR(optimizer, T_max=10)

    if resume_epoch == 0:
        print("Training {} from scratch...".format(modelName))
    else:
        checkpoint = torch.load(os.path.join(save_dir, 'models', saveName + '_epoch-' + str(resume_epoch - 1) + '.pth.tar'),
                       map_location=lambda storage, loc: storage)
        print("Initializing weights from: {}".format(
            os.path.join(save_dir, 'models', saveName + '_epoch-' + str(resume_epoch - 1) + '.pth.tar')))
        model.load_state_dict(checkpoint['state_dict'])
        optimizer.load_state_dict(checkpoint['opt_dict'])

    print('Total params: %.2fM' % (sum(p.numel() for p in model.parameters()) / 1000000.0))
    model.to(device)
    criterion.to(device)

    log_dir = os.path.join(save_dir, 'models', datetime.now().strftime('%b%d_%H-%M-%S') + '_' + socket.gethostname())
    writer = SummaryWriter(log_dir=log_dir)

    print('Training model on {} dataset...'.format(dataset))
    train_dataloader = DataLoader(VideoDataset(dataset=dataset, split='train', clip_len=5), batch_size=4, shuffle=True, num_workers=1)
    val_dataloader = DataLoader(VideoDataset(dataset=dataset, split='val', clip_len=5), batch_size=4, num_workers=1)
    test_dataloader = DataLoader(VideoDataset(dataset=dataset, split='test', clip_len=5), batch_size=4, num_workers=1)

    trainval_loaders = {'train': train_dataloader, 'val': val_dataloader}
    trainval_sizes = {x: len(trainval_loaders[x].dataset) for x in ['train', 'val']}
    test_size = len(test_dataloader.dataset)

    for epoch in range(resume_epoch, num_epochs):
        for phase in ['train', 'val']:
            start_time = timeit.default_timer()

            running_loss = 0.0
            running_corrects = 0.0
            running_probs = []
            running_labels = []

            if phase == 'train':
                scheduler.step()
                model.train()
            else:
                model.eval()

            for inputs, labels in tqdm(trainval_loaders[phase]):
                inputs = Variable(inputs, requires_grad=True).to(device)
                labels = Variable(labels).to(device)
                optimizer.zero_grad()
                if phase == 'train':
                    outputs = model(inputs)
                else:
                    with torch.no_grad():
                        outputs = model(inputs)

                probs = nn.Softmax(dim=1)(outputs)
                preds = torch.max(probs, 1)[1]
                loss = criterion(outputs, labels.long())

                if phase == 'train':
                    loss.backward()
                    optimizer.step()

                running_loss += loss.item() * inputs.size(0)
                running_corrects += torch.sum(preds == labels.data)

                running_probs.extend(probs[:, 1].tolist())
                running_labels.extend(labels.tolist())

            epoch_loss = running_loss / trainval_sizes[phase]
            epoch_acc = running_corrects.double() / trainval_sizes[phase]
            epoch_auc = roc_auc_score(running_labels, running_probs)
            epoch_sensitivity = recall_score(running_labels, [1 if p > 0.5 else 0 for p in running_probs])
            epoch_specificity = recall_score(running_labels, [1 if p > 0.5 else 0 for p in running_probs], pos_label=0)

            # 在每个阶段的循环结束后保存预测到CSV
            save_to_csv(epoch, phase, running_labels, running_probs, os.path.join(save_dir, 'predictions'))

            if phase == 'train':
                writer.add_scalar('data/train_loss_epoch', epoch_loss, epoch)
                writer.add_scalar('data/train_acc_epoch', epoch_acc, epoch)
                writer.add_scalar('data/train_auc_epoch', epoch_auc, epoch)
                writer.add_scalar('data/train_sensitivity_epoch', epoch_sensitivity, epoch)
                writer.add_scalar('data/train_specificity_epoch', epoch_specificity, epoch)
            else:
                writer.add_scalar('data/val_loss_epoch', epoch_loss, epoch)
                writer.add_scalar('data/val_acc_epoch', epoch_acc, epoch)
                writer.add_scalar('data/val_auc_epoch', epoch_auc, epoch)
                writer.add_scalar('data/val_sensitivity_epoch', epoch_sensitivity, epoch)
                writer.add_scalar('data/val_specificity_epoch', epoch_specificity, epoch)

            print("[{}] Epoch: {}/{} Loss: {} Acc: {} AUC: {} Sensitivity: {} Specificity: {}".format(
                phase, epoch + 1, nEpochs, epoch_loss, epoch_acc, epoch_auc, epoch_sensitivity, epoch_specificity))
            stop_time = timeit.default_timer()
            print("Execution time: " + str(stop_time - start_time) + "\n")

        if epoch % save_epoch == (save_epoch - 1):
            torch.save({
                'epoch': epoch + 1,
                'state_dict': model.state_dict(),
                'opt_dict': optimizer.state_dict(),
            }, os.path.join(save_dir, 'models', saveName + '_epoch-' + str(epoch) + '.pth.tar'))
            print("Save model at {}\n".format(os.path.join(save_dir, 'models', saveName + '_epoch-' + str(epoch) + '.pth.tar')))

        if useTest and epoch % test_interval == (test_interval - 1):
            model.eval()
            start_time = timeit.default_timer()

            running_loss = 0.0
            running_corrects = 0.0
            running_probs = []
            running_labels = []

            for inputs, labels in tqdm(test_dataloader):
                inputs = inputs.to(device)
                labels = labels.to(device)

                with torch.no_grad():
                    outputs = model(inputs)

                probs = nn.Softmax(dim=1)(outputs)
                preds = torch.max(probs, 1)[1]
                loss = criterion(outputs, labels.long())

                running_loss += loss.item() * inputs.size(0)
                running_corrects += torch.sum(preds == labels.data)

                running_probs.extend(probs[:, 1].tolist())
                running_labels.extend(labels.tolist())

            epoch_loss = running_loss / test_size
            epoch_acc = running_corrects.double() / test_size
            epoch_auc = roc_auc_score(running_labels, running_probs)
            epoch_sensitivity = recall_score(running_labels, [1 if p > 0.5 else 0 for p in running_probs])
            epoch_specificity = recall_score(running_labels, [1 if p > 0.5 else 0 for p in running_probs], pos_label=0)

            # 在测试阶段结束后保存预测到CSV
            save_to_csv(epoch, 'test', running_labels, running_probs, os.path.join(save_dir, 'predictions'))

            writer.add_scalar('data/test_loss_epoch', epoch_loss, epoch)
            writer.add_scalar('data/test_acc_epoch', epoch_acc, epoch)
            writer.add_scalar('data/test_auc_epoch', epoch_auc, epoch)
            writer.add_scalar('data/test_sensitivity_epoch', epoch_sensitivity, epoch)
            writer.add_scalar('data/test_specificity_epoch', epoch_specificity, epoch)

            print("[test] Epoch: {}/{} Loss: {} Acc: {} AUC: {} Sensitivity: {} Specificity: {}".format(
                epoch + 1, nEpochs, epoch_loss, epoch_acc, epoch_auc, epoch_sensitivity, epoch_specificity))
            stop_time = timeit.default_timer()
            print("Execution time: " + str(stop_time - start_time) + "\n")

    writer.close()


if __name__ == "__main__":
    train_model()