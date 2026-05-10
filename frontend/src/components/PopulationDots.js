import React from 'react';
import PropTypes from 'prop-types';
import styles from './PopulationDots.module.css';

/**
 * @typedef {0 | 1} Membership
 * @typedef {{ ip: Membership, den: Membership, denExc: Membership, num: Membership, numExc: Membership }} PopulationCounts
 */

const POP_KEYS = [
  { key: 'ip',     label: 'IP' },
  { key: 'den',    label: 'Denom' },
  { key: 'denExc', label: 'DnEx' },
  { key: 'num',    label: 'Numer' },
  { key: 'numExc', label: 'NmEx' },
];

const membershipShape = PropTypes.oneOf([0, 1]);
const countsShape = PropTypes.shape({
  ip:     membershipShape,
  den:    membershipShape,
  denExc: membershipShape,
  num:    membershipShape,
  numExc: membershipShape,
});

const POP_ARIA_LABELS = {
  ip:     'initial population',
  den:    'denominator',
  denExc: 'denominator exclusion',
  num:    'numerator',
  numExc: 'numerator exclusion',
};

/**
 * Compact 5-chip population summary. Pass `exp` to enable mismatch highlighting.
 * Filled chip = in population; hollow chip = not in population.
 * @param {{ act: PopulationCounts, exp?: PopulationCounts }} props
 */
export default function PopulationDots({ act, exp }) {
  const ariaLabel = POP_KEYS.map(({ key }) => {
    const a = act[key] ?? 0;
    return `${a ? 'In' : 'Not in'} ${POP_ARIA_LABELS[key]}`;
  }).join(', ');

  return (
    <div className={styles.row} aria-label={ariaLabel}>
      {POP_KEYS.map(({ key, label }) => {
        const a = act[key] ?? 0;
        const e = exp != null ? (exp[key] ?? 0) : null;
        const inPop = !!a;
        const miss = e !== null && a !== e;

        let chipCls = styles.chipDefault;
        if (miss) {
          chipCls = styles.chipMiss;
        } else if (inPop) {
          if (key === 'denExc' || key === 'numExc') chipCls = styles.chipWarn;
          else if (key === 'num') chipCls = styles.chipAccent;
          else chipCls = styles.chipIn;
        }

        return (
          <span key={key} className={`${styles.chip} ${chipCls}`}>
            {label}
            {miss && <span className={styles.exp}>/{e}</span>}
          </span>
        );
      })}
    </div>
  );
}

PopulationDots.propTypes = {
  act: countsShape.isRequired,
  exp: countsShape,
};
