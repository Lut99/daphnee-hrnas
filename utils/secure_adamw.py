import math
import crypten
import torch
import logging
# from torch.optim.optimizer import Optimizer
from crypten.optim.optimizer import Optimizer
from utils.common import index_tensor_in
from utils.common import check_tensor_in


class AdamW(Optimizer):
    r"""Implements AdamW algorithm.

    The original Adam algorithm was proposed in `Adam: A Method for Stochastic Optimization`_.
    The AdamW variant was proposed in `Decoupled Weight Decay Regularization`_.

    Arguments:
        params (iterable): iterable of parameters to optimize or dicts defining
            parameter groups
        lr (float, optional): learning rate (default: 1e-3)
        betas (Tuple[float, float], optional): coefficients used for computing
            running averages of gradient and its square (default: (0.9, 0.999))
        eps (float, optional): term added to the denominator to improve
            numerical stability (default: 1e-8)
        weight_decay (float, optional): weight decay coefficient (default: 1e-2)
        amsgrad (boolean, optional): whether to use the AMSGrad variant of this
            algorithm from the paper `On the Convergence of Adam and Beyond`_
            (default: False)

    .. _Adam\: A Method for Stochastic Optimization:
        https://arxiv.org/abs/1412.6980
    .. _Decoupled Weight Decay Regularization:
        https://arxiv.org/abs/1711.05101
    .. _On the Convergence of Adam and Beyond:
        https://openreview.net/forum?id=ryQu7f-RZ
    """

    def __init__(self, params, lr=1e-3, betas=(0.9, 0.999), eps=1e-8,
                 weight_decay=1e-2, amsgrad=False):
        if not 0.0 <= lr:
            raise ValueError("Invalid learning rate: {}".format(lr))
        if not 0.0 <= eps:
            raise ValueError("Invalid epsilon value: {}".format(eps))
        if not 0.0 <= betas[0] < 1.0:
            raise ValueError("Invalid beta parameter at index 0: {}".format(betas[0]))
        if not 0.0 <= betas[1] < 1.0:
            raise ValueError("Invalid beta parameter at index 1: {}".format(betas[1]))
        if not 0.0 <= weight_decay:
            raise ValueError("Invalid weight_decay value: {}".format(weight_decay))
        defaults = dict(lr=lr, betas=betas, eps=eps,
                        weight_decay=weight_decay, amsgrad=amsgrad)
        super(AdamW, self).__init__(params, defaults)

    def __setstate__(self, state):
        super(AdamW, self).__setstate__(state)
        for group in self.param_groups:
            group.setdefault('amsgrad', False)

    @torch.no_grad()
    def step(self, closure=None):
        """Performs a single optimization step.

        Arguments:
            closure (callable, optional): A closure that reevaluates the model
                and returns the loss.
        """

        with crypten.no_grad():
            loss = None
            if closure is not None:
                with torch.enable_grad():
                    with crypten.enable_grad():
                        loss = closure()

            for group in self.param_groups:
                for p in group['params']:
                    if p.grad is None:
                        continue

                    # Perform stepweight decay
                    p.mul_(1 - group['lr'] * group['weight_decay'])

                    # Perform optimization step
                    grad = p.grad
                    if grad.is_sparse:
                        raise RuntimeError('AdamW does not support sparse gradients')
                    amsgrad = group['amsgrad']

                    state = self.state[p]

                    # State initialization
                    if len(state) == 0:
                        state['step'] = 0
                        # Exponential moving average of gradient values
                        state['exp_avg'] = torch.zeros_like(p, memory_format=torch.preserve_format)
                        # Exponential moving average of squared gradient values
                        state['exp_avg_sq'] = torch.zeros_like(p, memory_format=torch.preserve_format)
                        if amsgrad:
                            # Maintains max of all exp. moving avg. of sq. grad. values
                            state['max_exp_avg_sq'] = torch.zeros_like(p, memory_format=torch.preserve_format)

                    exp_avg, exp_avg_sq = state['exp_avg'], state['exp_avg_sq']
                    if amsgrad:
                        max_exp_avg_sq = state['max_exp_avg_sq']
                    beta1, beta2 = group['betas']

                    state['step'] += 1
                    bias_correction1 = 1 - beta1 ** state['step']
                    bias_correction2 = 1 - beta2 ** state['step']

                    # Decay the first and second moment running average coefficient
                    exp_avg.mul_(beta1).add_(grad, alpha=1 - beta1)
                    exp_avg_sq.mul_(beta2).addcmul_(grad, grad, value=1 - beta2)
                    if amsgrad:
                        # Maintains the maximum of all 2nd moment running avg. till now
                        torch.max(max_exp_avg_sq, exp_avg_sq, out=max_exp_avg_sq)
                        # Use the max. for normalizing running avg. of gradient
                        denom = (max_exp_avg_sq.sqrt() / math.sqrt(bias_correction2)).add_(group['eps'])
                    else:
                        denom = (exp_avg_sq.sqrt() / math.sqrt(bias_correction2)).add_(group['eps'])

                    step_size = group['lr'] / bias_correction1

                    p.addcdiv_(exp_avg, denom, value=-step_size)

        return loss

    def compress_mask(self, info, verbose=False):
        """Adjust parameters values by masks for dynamic network shrinkage."""
        var_old = info['var_old']
        var_new = info['var_new']
        mask_hook = info['mask_hook']
        mask = info['mask']
        if verbose:
            logging.info('RMSProp compress: {} -> {}'.format(
                info['var_old_name'], info['var_new_name']))

        found = False
        for group in self.param_groups:
            index = index_tensor_in(var_old, group['params'], raise_error=False)
            found = index is not None
            if found:
                if check_tensor_in(var_old, self.state):
                    state = self.state.pop(var_old)
                    if len(state) != 0:  # generate new state
                        new_state = {'step': state['step']}
                        for key in ['exp_avg', 'exp_avg_sq', 'max_exp_avg_sq']:
                            if key in state:
                                new_state[key] = torch.zeros_like(
                                    var_new.data, device=var_old.device)
                                mask_hook(new_state[key], state[key], mask)
                                new_state[key].to(state[key].device)
                        self.state[var_new] = new_state

                # update group
                del group['params'][index]
                group['params'].append(var_new)
                break
        assert found, 'Var: {} not in RMSProp'.format(info['var_old_name'])

    def compress_drop(self, info, verbose=False):
        """Remove unused parameters for dynamic network shrinkage."""
        var_old = info['var_old']
        if verbose:
            logging.info('RMSProp drop: {}'.format(info['var_old_name']))

        assert info['type'] == 'variable'
        found = False
        for group in self.param_groups:
            index = index_tensor_in(var_old, group['params'], raise_error=False)
            found = index is not None
            if found:
                if check_tensor_in(var_old, self.state):
                    self.state.pop(var_old)
                del group['params'][index]
        assert found, 'Var: {} not in RMSProp'.format(info['var_old_name'])
