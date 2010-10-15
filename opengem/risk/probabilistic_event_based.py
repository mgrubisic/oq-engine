# -*- coding: utf-8 -*-

import numpy

from opengem import shapes

# TODO (ac): Does make sense to export numpy arrays?
def compute_loss_ratios(vuln_function, ground_motion_field):
    """Compute loss ratios using the ground motion field passed."""
    if vuln_function == shapes.EMPTY_CURVE or not ground_motion_field["IMLs"]:
        return []
    
    imls = vuln_function.abscissae
    loss_ratios = []
    
    # seems like with numpy you can only specify a single fill value
    # if the x_new is outside the range. Here we need two different values,
    # depending if the x_new is below or upon the defined values
    for ground_motion_value in ground_motion_field["IMLs"]:
        if ground_motion_value < imls[0]:
            loss_ratios.append(0.0)
        elif ground_motion_value > imls[-1]:
            loss_ratios.append(imls[-1])
        else:
            loss_ratios.append(vuln_function.ordinate_for(
                    ground_motion_value))
    
    return loss_ratios

def compute_loss_ratios_range(vuln_function):
    loss_ratios = vuln_function.ordinates[:, 0]
    return numpy.linspace(0.0, loss_ratios[-1], num=25)
    
def compute_cumulative_histogram(loss_ratios, loss_ratios_range):
    return list(numpy.histogram(loss_ratios, bins=loss_ratios_range)[0][::-1].cumsum()[::-1])

def compute_rates_of_exceedance(cum_histogram, ground_motion_field):
    return list(numpy.array(cum_histogram).astype(float)
            / ground_motion_field["TSES"])
