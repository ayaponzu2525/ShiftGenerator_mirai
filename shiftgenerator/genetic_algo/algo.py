import random

def generate_initial_population(staff_list, shifts, population_size):
    population = []
    for _ in range(population_size):
        individual = {}
        for shift in shifts:
            # 各シフトにランダムなスタッフを配置
            staff_for_shift = random.sample(staff_list, k=random.randint(1, len(staff_list)))
            individual[shift] = staff_for_shift
        population.append(individual)
    return population


#適応度関数
def fitness(individual, staff_data, shift_requirements):
    score = 0

    for shift, staff_in_shift in individual.items():
        required_staff = shift_requirements[shift]

        # スタッフのスキルや希望を考慮した評価
        for staff in staff_in_shift:
            staff_info = staff_data.get(staff)
            # 希望が反映されているか
            if shift in staff_info['desired_shifts']:
                score += 10
            else:
                score -= 5
            
            # レジや冷蔵スキルが必要か
            if shift in ['morning', 'afternoon', 'evening']:
                if staff_info['skills']['cold_storage']:
                    score += 5
                if staff_info['skills']['register'] and required_staff['register']:
                    score += 5
        
        # 必要な人数のチェック
        if len(staff_in_shift) < required_staff['min_staff']:
            score -= 20
        elif len(staff_in_shift) > required_staff['max_staff']:
            score -= 10

    return score

#選択　より良いものを選ぶ
def select_population(population, fitness_scores, num_selections):
    selected = random.choices(population, weights=fitness_scores, k=num_selections)
    return selected

#交叉
def crossover(parent1, parent2):
    child = {}
    for shift in parent1:
        if random.random() < 0.5:
            child[shift] = parent1[shift]
        else:
            child[shift] = parent2[shift]
    return child

#突然変異
def mutate(individual, staff_list):
    shift = random.choice(list(individual.keys()))
    new_staff = random.choice(staff_list)
    individual[shift].append(new_staff)


#アルゴリズムの実行
def genetic_algorithm(staff_list, shifts, population_size, generations, staff_data, shift_requirements):
    population = generate_initial_population(staff_list, shifts, population_size)

    for generation in range(generations):
        fitness_scores = [fitness(individual, staff_data, shift_requirements) for individual in population]

        # 適応度が高い順に選択
        selected_population = select_population(population, fitness_scores, population_size // 2)
        
        # 新しい世代を生成（交叉・突然変異）
        new_population = []
        while len(new_population) < population_size:
            parent1, parent2 = random.sample(selected_population, 2)
            child = crossover(parent1, parent2)
            mutate(child, staff_list)
            new_population.append(child)

        population = new_population

    # 最も適応度の高い個体を返す
    best_individual = max(population, key=lambda ind: fitness(ind, staff_data, shift_requirements))
    return best_individual
