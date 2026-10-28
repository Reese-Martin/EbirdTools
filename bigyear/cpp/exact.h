#pragma once
#include "model.h"
struct ExactResult {std::vector<int> route; bool optimal;};
ExactResult solve_exact(const Problem&,double);
